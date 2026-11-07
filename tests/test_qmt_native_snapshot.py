"""Synthetic 300319 native captures only; no QMT, network or production IO."""
import ast
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.qmt_native_snapshot import _decode, main, normalize_native_log, rebuild_from_native_log
from core.qmt_snapshot import CST, TABLES
from test_qmt_state_rebuild import snapshot

COLLECTOR = Path(__file__).resolve().parents[1] / "qmt_builtin" / "snapshot_collector.py"
ACCOUNT = "synthetic-account-id"
BASE_EPOCH = datetime(2026, 11, 6, 10, tzinfo=CST).timestamp()


def native_tables():
    common = dict(m_strAccountID=ACCOUNT, m_strInstrumentID="300319", m_strExchangeID="SZ")
    return dict(ACCOUNT=[dict(m_strAccountID=ACCOUNT, m_dAvailable=500000.0, m_dBalance=800000.0)],
                POSITION=[dict(common, m_nVolume=200, m_nCanUseVolume=200, m_dLastPrice=15.0)],
                ORDER=[], DEAL=[])


def native_order():
    return dict(m_strAccountID=ACCOUNT, m_strInstrumentID="300319", m_strExchangeID="SZ",
                m_strOrderSysID="contract-1", m_strInsertDate="20261106", m_strInsertTime="094000",
                m_nOffsetFlag=48, m_nDirection=48, m_nOrderStatus=56, m_nVolumeTotalOriginal=100,
                m_nVolumeTraded=100, m_dLimitPrice=15.0, m_strOrderRef="", m_strRemark="")


def native_deal():
    return dict(m_strAccountID=ACCOUNT, m_strInstrumentID="300319", m_strExchangeID="SZ",
                m_strOrderSysID="contract-1", m_strTradeID="fill-1", m_strTradeDate="20261106",
                m_strTradeTime="094100", m_nOffsetFlag=48, m_nDirection=48,
                m_nVolume=100, m_dPrice=15.0, m_dCommission=.1)


def namespace():
    ns = {}
    exec(compile(COLLECTOR.read_text(encoding="ascii"), str(COLLECTOR), "exec"), ns)
    return ns


def capture(tables=None, failure=None):
    ns, logs = namespace(), []
    ticks, calls = [], []
    def clock():
        ticks.append(1)
        return BASE_EPOCH + len(ticks) * .001
    def query(aid, asset, table):
        assert aid == ACCOUNT and asset == "stock"
        calls.append(table)
        value = (tables or native_tables())[table]
        if failure:
            value = failure(len(calls) - 1, table, value)
        return [SimpleNamespace(**row) if isinstance(row, dict) else row for row in value] if value is not None else None
    ns.update(ACCOUNT_ID_HARD=ACCOUNT, time=SimpleNamespace(time=clock),
              get_trade_detail_data=query, _w=lambda s: logs.append("[SNAP] " + s))
    ns["_collect"]()
    return "\n".join(logs) + "\n", datetime.fromtimestamp(clock() + .1, CST).isoformat(), calls


def normalize(text, now, **kwargs):
    return normalize_native_log(text, expected_account_id=ACCOUNT, account_scope="synthetic-scope", now=now, **kwargs)


def repack(packets):
    ns, lines = namespace(), []
    ns["_w"] = lambda s: lines.append("[SNAP] " + s)
    for kind, row in deepcopy(packets):
        if kind == "END":
            row.update(records=ns["_WIRE"]["n"], checksum=ns["_WIRE"]["checksum"])
        ns["_emit"](kind, row)
    return "\n".join(lines) + "\n"


def assert_halt(text, now, snapshot):
    original = deepcopy(snapshot)
    result = rebuild_from_native_log(text, snapshot, expected_account_id=ACCOUNT,
                                     account_scope="synthetic-scope", now=now)
    assert result["state"]["mode"] == "halt"
    assert result["state"]["can_open"] is result["state"]["can_reduce"] is False
    assert result["state"]["blockers"] and original == snapshot
    return result


def test_real_collector_output_feeds_existing_snapshot_and_rebuild_gate(snapshot):
    text, now, calls = capture()
    result = normalize(text, now)
    assert result["valid"] and calls == list(TABLES) * 2
    assert result["snapshot"]["atomic"] is False
    assert not result["ownership_verified"] and not result["history_complete"] and not result["fees_complete"]
    times = result["snapshot"]["read_windows"]
    assert len({r["finished_at"] for r in times}) == 8
    as_of = result["snapshot"]["as_of"]
    snapshot["as_of"] = snapshot["coverage"]["to"] = snapshot["risk"]["as_of"] = as_of
    original = deepcopy(snapshot)
    rebuilt = rebuild_from_native_log(text, snapshot, expected_account_id=ACCOUNT,
                                      account_scope="synthetic-scope", now=now)
    assert rebuilt["state"]["mode"] == "ready" and snapshot == original
    snapshot["anchor"]["verified"] = False
    assert assert_halt(text, now, snapshot)["state"]["blockers"] == ["anchor_unverified"]


@pytest.mark.parametrize("failed_index", range(8))
@pytest.mark.parametrize("failure_kind", ["none", "exception"])
def test_any_single_failed_query_makes_stable_false_and_halts(failed_index, failure_kind, snapshot):
    def failure(index, table, rows):
        if index == failed_index:
            if failure_kind == "exception":
                raise RuntimeError("synthetic broker failure, not an API call")
            return None
        return rows
    text, now, calls = capture(failure=failure)
    end = _decode(text)[-1][1]
    assert end["stable"] is False and end["errors"] and len(calls) == 8
    assert_halt(text, now, snapshot)


def test_both_rounds_all_none_never_pass_merely_because_they_are_equal(snapshot):
    text, now, _ = capture(failure=lambda *args: None)
    end = _decode(text)[-1][1]
    assert end["equal"] is True and end["stable"] is False and len(end["errors"]) == 8
    assert_halt(text, now, snapshot)


@pytest.mark.parametrize("table,field", [(t, k) for t, keys in namespace()["REQUIRED"].items() for k in keys])
def test_required_field_absent_in_identical_rounds_is_rejected(table, field, snapshot):
    tables = native_tables()
    tables["ORDER"], tables["DEAL"] = [native_order()], [native_deal()]
    tables[table][0].pop(field)
    text, now, _ = capture(tables)
    assert _decode(text)[-1][1]["stable"] is False
    assert_halt(text, now, snapshot)


def test_property_read_failure_is_not_silently_dropped(snapshot):
    class BadPosition:
        def __dir__(self):
            return list(native_tables()["POSITION"][0]) + ["m_bad"]
        def __getattr__(self, name):
            if name == "m_bad":
                raise RuntimeError("unreadable")
            return native_tables()["POSITION"][0][name]
    tables = native_tables()
    tables["POSITION"] = [BadPosition()]
    text, now, _ = capture(tables)
    assert _decode(text)[-1][1]["stable"] is False
    assert_halt(text, now, snapshot)


def test_native_numeric_subclasses_are_copied_as_primitives_without_guessed_values():
    class NativeInteger(int):
        def __repr__(self):
            return "binding.Enum(200)"
    tables = native_tables()
    tables["POSITION"][0]["m_nVolume"] = NativeInteger(200)
    text, now, _ = capture(tables)
    assert normalize(text, now)["valid"] and "binding.Enum" not in text


def test_unsupported_binding_object_is_rejected_instead_of_converted_to_string(snapshot):
    tables = native_tables()
    tables["POSITION"][0]["m_extra"] = object()
    text, now, _ = capture(tables)
    assert _decode(text)[-1][1]["stable"] is False
    assert_halt(text, now, snapshot)


@pytest.mark.parametrize("cut", [1, 5, 16, 100, 300])
def test_byte_truncation_in_end_record_cannot_unlock(cut, snapshot):
    text, now, _ = capture()
    assert_halt(text.rstrip("\n")[:-cut], now, snapshot)


@pytest.mark.parametrize("missing", [0, 1, 2, 4, -1])
def test_dropped_entire_record_is_detected_even_for_empty_tables(missing, snapshot):
    text, now, _ = capture()
    lines = text.splitlines()
    lines.pop(missing)
    assert_halt("\n".join(lines), now, snapshot)


def test_second_partial_run_does_not_fall_back_to_first_success(snapshot):
    text, now, _ = capture()
    assert_halt(text + text.splitlines()[0] + "\n", now, snapshot)


def test_sequence_checksum_and_duplicate_rows_are_validated(snapshot):
    text, now, _ = capture()
    lines = text.splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    assert_halt("\n".join(lines), now, snapshot)
    lines = text.splitlines()
    lines.insert(2, lines[2])
    assert_halt("\n".join(lines), now, snapshot)
    packets = _decode(text)
    packets[2][1]["f"] += 1
    assert_halt(repack(packets), now, snapshot)


def test_payload_change_without_matching_seal_is_rejected(snapshot):
    text, now, _ = capture()
    # A same-length native value change retains sequence and row counts.
    changed = text.replace(ascii("synthetic-account-id").encode("ascii").hex(),
                           ascii("synthetic-account-ix").encode("ascii").hex(), 1)
    assert changed != text
    assert "native_seal_mismatch" in assert_halt(changed, now, snapshot)["state"]["blockers"]


def test_null_required_field_is_reported_by_collector_not_just_normalizer(snapshot):
    tables = native_tables()
    tables["POSITION"][0]["m_nVolume"] = None
    text, now, _ = capture(tables)
    assert _decode(text)[-1][1]["stable"] is False
    assert_halt(text, now, snapshot)


@pytest.mark.parametrize("marker", ["[SNAP] V2", "[SNAP] V3 n=0", "[SNAP] V2 n=0 kind=BEG"])
def test_partial_or_unsupported_version_marker_is_rejected(marker, snapshot):
    text, now, _ = capture()
    assert_halt(text + marker, now, snapshot)


def test_forged_stable_true_does_not_hide_query_none(snapshot):
    text, now, _ = capture(failure=lambda *args: None)
    packets = _decode(text)
    packets[-1][1].update(stable=True, equal=True, errors=[])
    assert_halt(repack(packets), now, snapshot)


def test_forged_stable_true_does_not_hide_different_rounds(snapshot):
    text, now, _ = capture()
    packets = _decode(text)
    for kind, row in packets:
        if kind == "D" and row["r"] == 2 and row["t"] == "POSITION":
            row["fields"] = [(k, 300 if k == "m_nVolume" else v) for k, v in row["fields"]]
    result = assert_halt(repack(packets), now, snapshot)
    assert result["native"]["blockers"] == ["changed_position"]


@pytest.mark.parametrize("key", ["t0", "t1", "api_t1", "ms", "api_ms"])
def test_bad_actual_query_windows_are_rejected_without_retiming(key, snapshot):
    text, now, _ = capture()
    packets = _decode(text)
    query = packets[1][1]
    query[key] = query[key] + (10 if key in ("ms", "api_ms") else 2)
    assert_halt(repack(packets), now, snapshot)


def test_missing_native_field_is_checked_even_if_collector_falsely_reports_success(snapshot):
    text, now, _ = capture()
    packets = _decode(text)
    for kind, row in packets:
        if kind == "D" and row["t"] == "POSITION":
            row["fields"] = [(k, v) for k, v in row["fields"] if k != "m_nVolume"]
            row["f"] -= 1
    assert_halt(repack(packets), now, snapshot)


def test_whitespace_unicode_delimiter_and_fake_record_in_remark_roundtrip_without_owner_inference(snapshot):
    tables = native_tables()
    remark = "manual? 中文 空格,~= x=1\n[SNAP] V2 n=0 kind=BEGIN data=00"
    tables["ORDER"] = [dict(native_order(), m_strRemark=remark)]
    text, now, _ = capture(tables)
    assert text.isascii()
    result = normalize(text, now)
    assert result["valid"]
    order = result["bundle"]["cycles"][0]["reads"][2]["data"][0]
    assert order["native_remark"] == remark and order["owner"] is None and order["reason"] is None
    as_of = result["snapshot"]["as_of"]
    snapshot["as_of"] = snapshot["coverage"]["to"] = snapshot["risk"]["as_of"] = as_of
    assert "order_owner_unverified" in assert_halt(text, now, snapshot)["state"]["blockers"]


def test_commission_is_kept_separate_and_does_not_become_all_in_fee():
    tables = native_tables()
    tables["ORDER"], tables["DEAL"] = [native_order()], [native_deal()]
    text, now, _ = capture(tables)
    result = normalize(text, now)
    assert result["valid"]
    deal = result["bundle"]["cycles"][0]["reads"][3]["data"][0]
    assert deal["fee"] is None and deal["native_commission"] == .1


@pytest.mark.parametrize("missing", ["anchor", "coverage", "risk"])
def test_native_success_does_not_approve_missing_business_evidence(missing, snapshot):
    text, now, _ = capture()
    as_of = normalize(text, now)["snapshot"]["as_of"]
    snapshot["as_of"] = snapshot["coverage"]["to"] = snapshot["risk"]["as_of"] = as_of
    snapshot[missing] = None
    result = assert_halt(text, now, snapshot)
    assert result["native"]["valid"]
    assert result["state"]["blockers"] != ["snapshot_rebuild_template_invalid"]


@pytest.mark.parametrize("change", ["position", "order_count"])
def test_actual_native_changes_never_pass_the_gate(change, snapshot):
    def second(index, table, rows):
        rows = deepcopy(rows)
        if index == 5 and change == "position":
            rows[0]["m_nVolume"] = 300
        if index == 6 and change == "order_count":
            rows.append(native_order())
        return rows
    text, now, _ = capture(failure=second)
    assert _decode(text)[-1][1]["stable"] is False
    assert_halt(text, now, snapshot)


def test_actual_epoch_windows_cover_field_reads_and_do_not_use_machine_timezone():
    text, now, _ = capture()
    queries = [row for kind, row in _decode(text) if kind == "Q"]
    result = normalize(text, now)
    for query, window in zip(queries, result["snapshot"]["read_windows"]):
        assert query["t0"] <= query["api_t1"] < query["t1"]
        assert datetime.fromisoformat(window["finished_at"]).timestamp() == query["t1"]
        assert window["finished_at"].endswith("+08:00")


def test_sell_direction_uses_offset_not_stock_direction_field():
    tables = native_tables()
    tables["ORDER"] = [dict(native_order(), m_nOffsetFlag=49, m_nDirection=48)]
    text, now, _ = capture(tables)
    row = normalize(text, now)["bundle"]["cycles"][0]["reads"][2]["data"][0]
    assert row["side"] == "SELL"


@pytest.mark.parametrize("status,normalized", [(49, "pending_submit"), (50, "working"), (51, "working"),
    (52, "partial"), (53, "cancelled"), (54, "cancelled"), (55, "partial"), (56, "filled"), (57, "rejected")])
def test_official_status_mapping_keeps_cancel_requests_active(status, normalized):
    tables = native_tables()
    tables["ORDER"] = [dict(native_order(), m_nOrderStatus=status)]
    text, now, _ = capture(tables)
    row = normalize(text, now)["bundle"]["cycles"][0]["reads"][2]["data"][0]
    assert row["status"] == normalized


@pytest.mark.parametrize("table,field,value", [
    ("POSITION", "m_nVolume", True), ("POSITION", "m_nCanUseVolume", 201),
    ("POSITION", "m_dLastPrice", float("nan")), ("POSITION", "m_dLastPrice", 1.79e308),
    ("ORDER", "m_nOrderStatus", 999), ("ORDER", "m_nOffsetFlag", 24),
    ("ORDER", "m_strInsertTime", "bad"), ("DEAL", "m_strTradeDate", "20261301"),
    ("ACCOUNT", "m_dAvailable", -1), ("ACCOUNT", "m_strAccountID", "wrong-account")])
def test_invalid_native_values_halt(table, field, value, snapshot):
    tables = native_tables()
    tables["ORDER"], tables["DEAL"] = [native_order()], [native_deal()]
    tables[table][0][field] = value
    text, now, _ = capture(tables)
    assert_halt(text, now, snapshot)


def test_target_position_with_shares_and_sentinel_price_still_fails_by_name(snapshot):
    tables = native_tables()
    tables["POSITION"][0]["m_dLastPrice"] = 1.7976931348623157e+308
    text, now, _ = capture(tables)
    result = normalize(text, now)
    assert not result["valid"] and result["bundle"] is None
    assert result["blockers"] == ["native_target_mark_price_invalid"]


def test_zero_volume_residual_row_sentinel_price_never_blocks_the_target(snapshot):
    """Real 2026-11-07 shape: a matured reverse-repo row keeps volume 0 and
    m_dLastPrice = DBL_MAX. It must stay in evidence, unpriced, and must not
    invalidate the whole capture while the target itself is priced."""
    tables = native_tables()
    tables["POSITION"].append(dict(m_strAccountID=ACCOUNT, m_strInstrumentID="131990",
                                   m_strExchangeID="SZ", m_nVolume=0, m_nCanUseVolume=0,
                                   m_dLastPrice=1.7976931348623157e+308,
                                   m_dAvgOpenPrice=1.7976931348623157e+308,
                                   m_dOpenPrice=0.0))
    text, now, _ = capture(tables)
    result = normalize(text, now)
    assert result["valid"], result["blockers"]
    assert result["unpriced"] == ["131990.SZ"]
    rows = {row["symbol"]: row for row in result["snapshot"]["data"]["POSITION"]}
    assert rows["131990.SZ"]["quantity"] == 0 and rows["131990.SZ"]["mark_price"] is None
    assert rows["300319.SZ"]["mark_price"] == 15.0


def test_non_target_shares_with_sentinel_price_stay_unpriced_not_zeroed(snapshot):
    tables = native_tables()
    tables["POSITION"].append(dict(m_strAccountID=ACCOUNT, m_strInstrumentID="601975",
                                   m_strExchangeID="SH", m_nVolume=3900, m_nCanUseVolume=3900,
                                   m_dLastPrice=1.7976931348623157e+308))
    text, now, _ = capture(tables)
    result = normalize(text, now)
    assert result["valid"], result["blockers"]
    rows = {row["symbol"]: row for row in result["snapshot"]["data"]["POSITION"]}
    assert rows["601975.SH"]["quantity"] == 3900
    assert rows["601975.SH"]["mark_price"] is None


def test_account_binding_does_not_trust_masked_or_caller_relabelled_account(snapshot):
    text, now, _ = capture()
    result = normalize_native_log(text, expected_account_id="wrong", account_scope="synthetic-scope", now=now)
    assert not result["valid"] and result["bundle"] is None


def test_stale_captured_time_is_never_replaced_by_ingestion_time(snapshot):
    text, now, _ = capture()
    future_now = (datetime.fromisoformat(now) + timedelta(seconds=10)).isoformat()
    assert "stale_snapshot" in assert_halt(text, future_now, snapshot)["state"]["blockers"]


def test_unmodified_legacy_v1_is_rejected_and_keeps_halt(snapshot):
    text = "[SNAP] BEGIN asof_epoch=1793942780\n[SNAP] END equal=1\n[SNAP] VERDICT stable=yes\n"
    result = assert_halt(text, datetime.fromtimestamp(BASE_EPOCH, CST).isoformat(), snapshot)
    assert result["native"]["blockers"] == ["native_v2_required_legacy_not_upgraded"]


def test_main_input_failure_is_safe_and_does_not_print_account(tmp_path, capsys):
    account_file = tmp_path / "account.txt"
    account_file.write_text(ACCOUNT, encoding="utf-8")
    log = tmp_path / "log.txt"
    log.write_text("[SNAP] END equal=1", encoding="ascii")
    output = tmp_path / "result.json"
    code = main(["--input", str(log), "--account-file", str(account_file),
                 "--account-scope", "synthetic", "--out", str(output)])
    assert code == 2 and json.loads(output.read_text(encoding="utf-8"))["state"]["mode"] == "halt"
    assert ACCOUNT not in capsys.readouterr().out


def test_sandbox_constraints_and_all_24_order_functions_still_blocked():
    source = COLLECTOR.read_bytes()
    assert source.isascii()
    tree = ast.parse(source)
    assert [n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)] == ["time"]
    assert not any(isinstance(n, ast.ImportFrom) for n in ast.walk(tree))
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"open", "exec", "eval", "__import__"}
    ns = namespace()
    original_called = []
    for name in ns["ORDER_FNS"]:
        ns[name] = lambda *args: original_called.append(args)
    ns["_w"] = lambda text: None
    ns["init"](SimpleNamespace())
    assert len(ns["_ST"]["guard"]) == 24
    for name in ns["ORDER_FNS"]:
        with pytest.raises(RuntimeError):
            ns[name]()
    assert original_called == []
