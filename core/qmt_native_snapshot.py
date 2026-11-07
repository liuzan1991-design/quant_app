"""External-only native V2 log decoder. No broker sessions or order calls.

Parsing a complete capture never certifies ownership, history, fees or risk.
The log checksum detects loss/damage, not authenticity of the source.
"""
import argparse
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from core.qmt_snapshot import CST, TABLES, number, rebuild_from_snapshot, validate_snapshot
from qmt_builtin.snapshot_collector import REQUIRED

MARKER = "[SNAP] V2 "
LINE = re.compile(r"V2 n=(\d+) kind=(BEGIN|Q|D|END) data=([0-9a-f]+)")
# Official innerApi enums; unknown values are never treated as final orders.
STATUSES = {49: "pending_submit", 50: "working", 51: "working", 52: "partial",
            53: "cancelled", 54: "cancelled", 55: "partial", 56: "filled", 57: "rejected"}


def _require(condition, code):
    if not condition:
        raise ValueError(code)


def _integer(value):
    _require(type(value) is int and 0 <= value < 10**12, "native_invalid_integer")
    return value


def _string(value):
    _require(isinstance(value, str) and bool(value.strip()) and value == value.strip(),
             "native_missing_identity")
    return value


def _epoch(value):
    number(value, positive=True)
    return datetime.fromtimestamp(value, CST).isoformat()


def _decode(text):
    """Exactly one full run per input; never fall back to an older good run."""
    _require(isinstance(text, str), "native_log_not_text")
    packets, checksum = [], 0
    for line in text.splitlines():
        if MARKER not in line:
            # A damaged version marker must not disappear as unrelated logging.
            _require(re.search(r"\[SNAP\] V(?:\d|$)", line) is None, "native_bad_version_or_record")
            continue
        wire = line[line.index(MARKER) + len("[SNAP] "):]
        match = LINE.fullmatch(wire)
        _require(match is not None, "native_malformed_or_truncated_record")
        seq, kind, payload = match.groups()
        _require(int(seq) == len(packets), "native_sequence_or_multiple_runs")
        try:
            obj = ast.literal_eval(bytes.fromhex(payload).decode("ascii"))
        except (ValueError, SyntaxError, UnicodeError, TypeError, RecursionError):
            raise ValueError("native_payload_invalid") from None
        _require(type(obj) is dict, "native_payload_not_object")
        json.dumps(obj, allow_nan=False)
        if kind == "END":
            _require(obj.get("records") == len(packets) and type(obj.get("records")) is int
                     and obj.get("checksum") == checksum and type(obj.get("checksum")) is int,
                     "native_seal_mismatch")
        _require(not packets or packets[-1][0] != "END", "native_records_after_end")
        packets.append((kind, obj))
        for char in wire:
            checksum = (checksum * 33 + ord(char)) & 0xffffffff
    _require(bool(packets), "native_v2_required_legacy_not_upgraded")
    _require(packets[0][0] == "BEGIN" and packets[-1][0] == "END", "native_log_truncated")
    _require(sum(kind == "BEGIN" for kind, _ in packets) == 1, "native_multiple_runs")
    return packets


def _symbol(row):
    exchange, code = _string(row["m_strExchangeID"]), _string(row["m_strInstrumentID"])
    _require(exchange in ("SH", "SZ", "BJ") and re.fullmatch(r"\d{6}", code),
             "native_unsupported_security")
    return code + "." + exchange


def _event_time(row, date_key, time_key):
    date, time = row[date_key], row[time_key]
    _require(isinstance(date, str) and re.fullmatch(r"\d{8}", date)
             and isinstance(time, str) and re.fullmatch(r"\d{6}", time), "native_event_time_invalid")
    try:
        return datetime.strptime(date + time, "%Y%m%d%H%M%S").replace(tzinfo=CST).isoformat()
    except ValueError:
        raise ValueError("native_event_time_invalid") from None


def _position_price(row, symbol, quantity, target):
    """DBL_MAX / NaN / <=0 is a native sentinel, never a usable price.

    A zero-quantity residual row (a matured reverse repo, for example)
    carries that sentinel on this counter and must not invalidate the whole
    capture. The target itself, while it holds shares, must still fail here:
    its price drives sizing and the stop. Missing is never replaced by 0.
    """
    try:
        return number(row["m_dLastPrice"], positive=True)
    except ValueError:
        if symbol == target and quantity > 0:
            raise ValueError("native_target_mark_price_invalid") from None
        return None


def _normalize_row(table, row, expected_account_id, target, unpriced):
    _require(all(key in row and row[key] is not None for key in REQUIRED[table]),
             "native_required_field_missing_" + table.lower())
    _require(row["m_strAccountID"] == expected_account_id, "native_row_account_mismatch")
    # Compare ALL raw fields without copying private account keys into the bundle.
    fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=True,
                                            allow_nan=False).encode("ascii")).hexdigest()
    common = dict(native_fingerprint=fingerprint)
    if table == "ACCOUNT":
        return dict(common, available=number(row["m_dAvailable"]),
                    total_equity=number(row["m_dBalance"], positive=True))
    symbol = _symbol(row)
    if table == "POSITION":
        quantity = _integer(row["m_nVolume"])
        sellable = _integer(row["m_nCanUseVolume"])
        _require(sellable <= quantity, "native_invalid_sellable")
        price = _position_price(row, symbol, quantity, target)
        if price is None:
            unpriced.append(symbol)
        return dict(common, symbol=symbol, quantity=quantity, sellable_quantity=sellable,
                    mark_price=price)
    offset = row["m_nOffsetFlag"]
    _require(type(offset) is int and offset in (48, 49), "native_unsupported_side")
    common.update(symbol=symbol, side="BUY" if offset == 48 else "SELL",
                  order_id=symbol + ":" + _string(row["m_strOrderSysID"]),
                  native_order_ref=row.get("m_strOrderRef"), native_remark=row.get("m_strRemark"))
    if table == "ORDER":
        quantity, filled = _integer(row["m_nVolumeTotalOriginal"]), _integer(row["m_nVolumeTraded"])
        _require(quantity > 0 and filled <= quantity, "native_order_quantity_invalid")
        status = row["m_nOrderStatus"]
        _require(type(status) is int and status in STATUSES, "native_order_status_unknown")
        _require(status != 56 or filled == quantity, "native_filled_order_quantity_conflict")
        return dict(common, time=_event_time(row, "m_strInsertDate", "m_strInsertTime"),
                    quantity=quantity, filled_quantity=filled, status=STATUSES[status],
                    limit_price=number(row["m_dLimitPrice"]), owner=None, reason=None, fee_reserve=None)
    quantity = _integer(row["m_nVolume"])
    _require(quantity > 0, "native_deal_quantity_invalid")
    event = _event_time(row, "m_strTradeDate", "m_strTradeTime")
    return dict(common, trade_id=symbol + ":" + row["m_strTradeDate"] + ":" + _string(row["m_strTradeID"]),
                time=event, quantity=quantity, price=number(row["m_dPrice"], positive=True), fee=None,
                native_commission=row.get("m_dCommission"))


def normalize_native_log(text, *, expected_account_id, account_scope, now,
                         symbol="300319.SZ", **limits):
    """Decode one capture and apply the existing stable/freshness gate.

    complete=True means this *observed table* was transmitted in full. It
    never sets any rebuild coverage flag or proves broker historical coverage.
    """
    result = dict(valid=False, blockers=[], bundle=None, snapshot=None, source_version=2,
                  ownership_verified=False, history_complete=False, fees_complete=False,
                  unpriced=[])
    unpriced = []
    try:
        _string(expected_account_id)
        _string(account_scope)
        _require(symbol == "300319.SZ", "native_300319_acceptance_only")
        packets = _decode(text)
        begin, end = packets[0][1], packets[-1][1]
        _require(begin.get("version") == 2 and type(begin.get("version")) is int
                 and tuple(begin["tables"]) == TABLES and begin.get("rounds") == 2,
                 "native_contract_mismatch")
        _require(begin["account"] == expected_account_id, "native_account_mismatch")
        _require(begin["code"] == symbol, "native_symbol_mismatch")
        _epoch(begin["epoch"])
        _require(end.get("errors") == [] and end.get("stable") is True
                 and end.get("equal") is True, "native_capture_failed_or_changed")
        cursor, cycles, timings = 1, [], []
        for rnd in (1, 2):
            reads = []
            for table in TABLES:
                kind, query = packets[cursor]
                cursor += 1
                _require(kind == "Q" and query["r"] == rnd and type(query["r"]) is int
                         and query["t"] == table, "native_query_order")
                _require(query["st"] == "OK" and query.get("errors") == [], "native_query_failed")
                count = _integer(query["n"])
                _require(count <= 100000 and (table != "ACCOUNT" or count == 1), "native_row_count_invalid")
                start, finish = _epoch(query["t0"]), _epoch(query["t1"])
                _integer(query["ms"])
                _epoch(query["api_t1"])
                _integer(query["api_ms"])
                _require(query["t0"] <= query["api_t1"] <= query["t1"], "native_api_time_conflict")
                _require(abs(query["ms"] - int((query["t1"] - query["t0"]) * 1000)) <= 1,
                         "native_query_timing_conflict")
                _require(abs(query["api_ms"] - int((query["api_t1"] - query["t0"]) * 1000)) <= 1,
                         "native_api_timing_conflict")
                timings.append({key: query[key] for key in ("r", "t", "n", "st", "t0", "t1", "ms", "api_t1", "api_ms")})
                rows = []
                for index in range(count):
                    kind, data = packets[cursor]
                    cursor += 1
                    _require(kind == "D" and data["r"] == rnd and type(data["r"]) is int
                             and data["t"] == table and _integer(data["k"]) == index,
                             "native_row_order")
                    pairs = data["fields"]
                    _require(isinstance(pairs, list) and len(pairs) == _integer(data["f"]),
                             "native_field_count_mismatch")
                    row = {}
                    for key, value in pairs:
                        _require(isinstance(key, str) and re.fullmatch(r"m_\w+", key)
                                 and key not in row and type(value) in (str, int, float, bool, type(None)),
                                 "native_field_invalid_or_duplicate")
                        row[key] = value
                    rows.append(_normalize_row(table, row, expected_account_id, symbol, unpriced))
                reads.append(dict(table=table, account_scope=account_scope, complete=True,
                                  started_at=start, finished_at=finish,
                                  data=rows[0] if table == "ACCOUNT" else rows))
            cycles.append(dict(reads=reads))
        _require(cursor == len(packets) - 1, "native_unexpected_records")
        first_time = cycles[0]["reads"][0]["started_at"]
        last_time = cycles[-1]["reads"][-1]["finished_at"]
        _require(_epoch(begin["epoch"]) <= first_time <= last_time <= _epoch(end["finished_epoch"]),
                 "native_capture_time_conflict")
        bundle = dict(account_scope=account_scope, cycles=cycles)
        report = validate_snapshot(bundle, now=now, **limits)
        result.update(valid=report["valid"], blockers=report["blockers"], snapshot=report,
                      bundle=bundle if report["valid"] else None, query_timings=timings,
                      unpriced=sorted(set(unpriced)))
    except (ValueError, KeyError, IndexError, TypeError, OverflowError, OSError) as error:
        # No exception text from native payloads/IDs is emitted to logs.
        code = str(error) if isinstance(error, ValueError) and str(error).startswith("native_") else "native_invalid_schema"
        result.update(valid=False, blockers=[code], bundle=None, unpriced=sorted(set(unpriced)))
    return result


def rebuild_from_native_log(text, template, *, expected_account_id, account_scope, now, **limits):
    """Native log -> snapshot gate -> existing pure state rebuild. No approvals."""
    native = normalize_native_log(text, expected_account_id=expected_account_id,
                                  account_scope=account_scope, now=now, **limits)
    halted = dict(mode="halt", can_open=False, can_reduce=False, blockers=native["blockers"],
                  t_total_shares=None)
    if not native["valid"]:
        return dict(native=native, state=halted)
    result = rebuild_from_snapshot(native["bundle"], template, now=now, **limits)
    return dict(native=native, **result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--account-file", required=True, help="Local account ID file; never print its contents")
    parser.add_argument("--account-scope", required=True, help="Anonymous scope label")
    parser.add_argument("--template", help="Existing evidence; never rewritten or auto-approved")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        text = Path(args.input).read_bytes().decode("ascii", errors="replace")
        account = Path(args.account_file).read_text(encoding="utf-8-sig").strip()
        kwargs = dict(expected_account_id=account, account_scope=args.account_scope,
                      now=datetime.now(CST).isoformat())
        if args.template:
            result = rebuild_from_native_log(text, json.loads(Path(args.template).read_text(encoding="utf-8-sig")), **kwargs)
        else:
            native = normalize_native_log(text, **kwargs)
            result = dict(native=native, state=dict(mode="halt", can_open=False, can_reduce=False,
                          blockers=native["blockers"] or ["native_evidence_only_template_required"]))
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps({"mode": result["state"]["mode"], "blockers": result["state"]["blockers"]}))
        return 0 if result["state"]["mode"] == "ready" else 2
    except (OSError, ValueError, TypeError):
        print('{"mode":"halt","blockers":["native_input_or_output_error"]}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
