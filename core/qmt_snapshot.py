"""Read-only double-read snapshot validation, separate from the QMT runtime."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math

from qmt_builtin.state_rebuild import rebuild_state

TABLES = ("ACCOUNT", "POSITION", "ORDER", "DEAL")
KEYS = {"POSITION": "symbol", "ORDER": "order_id", "DEAL": "trade_id"}
CST = timezone(timedelta(hours=8))


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed.astimezone(CST)


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid_number")
    if abs(value) >= 1e12 or (value <= 0 if positive else value < 0):
        raise ValueError("invalid_number")
    return value


def _payload(table, data):
    # Reject NaN/Infinity anywhere, including otherwise unused native fields.
    json.dumps(data, allow_nan=False)
    if table == "ACCOUNT":
        if not isinstance(data, dict):
            raise ValueError("account_not_object")
        number(data["available"])
        return data
    if not isinstance(data, list):
        raise ValueError("table_not_list")
    seen = set()
    for row in data:
        key = row[KEYS[table]]
        if not isinstance(key, str) or not key.strip() or key in seen:
            raise ValueError("missing_or_duplicate_identity")
        seen.add(key)
        if table == "POSITION":
            quantity, sellable = number(row["quantity"]), number(row["sellable_quantity"])
            if int(quantity) != quantity or int(sellable) != sellable or sellable > quantity:
                raise ValueError("invalid_position_quantity")
        else:
            timestamp(row["time"])
    return sorted(data, key=lambda row: row[KEYS[table]])


def _hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def validate_snapshot(bundle, *, now, max_span_seconds=3.0, max_age_seconds=5.0):
    """Compare two full, sequential reads, retaining real read windows.

    Stable repeated values are optimistic consistency evidence, not proof of
    an atomic broker transaction. This never verifies ownership or coverage.
    """
    result = dict(valid=False, blockers=[], as_of=None, data=None, read_windows=[],
                  fingerprints={}, consistency_model="stable_double_read", atomic=False)
    try:
        number(max_span_seconds, positive=True)
        number(max_age_seconds, positive=True)
        current = timestamp(now)
        cycles = bundle["cycles"]
        expected_scope = bundle["account_scope"]
        if not isinstance(expected_scope, str) or not expected_scope.strip():
            raise ValueError("account_scope_missing")
        if not isinstance(cycles, list) or len(cycles) != 2:
            raise ValueError("two_cycles_required")
        scopes, payloads, starts, ends = set(), [], [], []
        previous_end = None
        for cycle_index, cycle in enumerate(cycles):
            reads = cycle["reads"]
            if len(reads) != len(TABLES) or tuple(row["table"] for row in reads) != TABLES:
                raise ValueError("four_tables_in_order_required")
            tables = {}
            for row in reads:
                if row.get("complete") is not True:
                    raise ValueError("table_read_incomplete")
                start, end = timestamp(row["started_at"]), timestamp(row["finished_at"])
                if end < start or (previous_end is not None and start < previous_end):
                    raise ValueError("read_windows_not_sequential")
                if end > current:
                    raise ValueError("future_snapshot")
                previous_end = end
                scope = row["account_scope"]
                if not isinstance(scope, str) or not scope.strip():
                    raise ValueError("account_scope_missing")
                scopes.add(scope)
                table = row["table"]
                tables[table] = _payload(table, row["data"])
                starts.append(start)
                ends.append(end)
                result["read_windows"].append(dict(cycle=cycle_index, table=table,
                    started_at=start.isoformat(), finished_at=end.isoformat()))
            payloads.append(tables)
        if scopes != {expected_scope}:
            raise ValueError("account_scope_mismatch")
        span = (ends[-1] - starts[0]).total_seconds()
        age = (current - ends[-1]).total_seconds()
        result.update(span_seconds=span, age_seconds=age, account_scope=next(iter(scopes)))
        if span <= 0 or span > max_span_seconds:
            raise ValueError("snapshot_span_exceeded_or_zero")
        if age > max_age_seconds:
            raise ValueError("stale_snapshot")
        if starts[0].date() != ends[-1].date():
            raise ValueError("snapshot_crosses_trade_date")
        for table in TABLES:
            first, second = _hash(payloads[0][table]), _hash(payloads[1][table])
            result["fingerprints"][table] = [first, second]
            if first != second:
                result["blockers"].append("changed_" + table.lower())
        # Equal ending quantities must not hide transactions during collection.
        for table in ("ORDER", "DEAL"):
            for row in payloads[1][table]:
                event = timestamp(row["time"])
                if event > ends[-1]:
                    result["blockers"].append("future_" + table.lower() + "_event")
                elif event >= starts[0]:
                    result["blockers"].append("event_during_collection")
        result["blockers"] = sorted(set(result["blockers"]))
        if not result["blockers"]:
            result.update(valid=True, as_of=ends[-1].isoformat(), data=deepcopy(payloads[1]))
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        reason = str(error) if isinstance(error, ValueError) else "invalid_snapshot_schema"
        result["blockers"].append(reason)
    return result


def collect_consistent_snapshot(read_table, *, account_scope, clock=None, max_attempts=3,
                                max_span_seconds=3.0, max_age_seconds=5.0):
    """Invoke an injected READ-ONLY adapter, with bounded retry and no sleeps.

    This module never creates a broker session. The adapter returns an envelope
    with account_scope, complete and copied normalized data. No native objects.
    """
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts_must_be_1_to_10")
    number(max_span_seconds, positive=True)
    number(max_age_seconds, positive=True)
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    attempts = []
    for attempt in range(max_attempts):
        cycles = []
        try:
            for _ in range(2):
                reads = []
                cycles.append({"reads": reads})
                for table in TABLES:
                    start = clock()
                    envelope = deepcopy(read_table(table))
                    reads.append(dict(table=table, account_scope=envelope["account_scope"],
                                      complete=envelope["complete"], started_at=start,
                                      finished_at=clock(), data=envelope["data"]))
            bundle = {"account_scope": account_scope, "cycles": cycles}
            report = validate_snapshot(bundle, now=clock(), max_span_seconds=max_span_seconds,
                                       max_age_seconds=max_age_seconds)
        except Exception as error:
            bundle = {"account_scope": account_scope, "cycles": cycles}
            report = dict(valid=False, blockers=["read_adapter_error"], error_type=type(error).__name__,
                          as_of=None, data=None, atomic=False)
        attempts.append(dict(attempt=attempt + 1, report=report, bundle=bundle))
        if report["valid"]:
            return dict(report=report, bundle=bundle, attempts=attempts)
    return dict(report=report, bundle=bundle, attempts=attempts)


def rebuild_from_snapshot(bundle, template, *, now, **limits):
    """Gate this new read-only pipeline; never retime or approve risk evidence."""
    report = validate_snapshot(bundle, now=now, **limits)
    blocked = dict(mode="halt", can_open=False, can_reduce=False, blockers=[], t_total_shares=None)
    if not report["valid"]:
        blocked["blockers"] = ["snapshot_" + reason for reason in report["blockers"]]
        return dict(snapshot=report, state=blocked)
    inputs = deepcopy(template)
    try:
        if timestamp(inputs["as_of"]) != timestamp(report["as_of"]):
            raise ValueError("snapshot_as_of_mismatch")
        data = report["data"]
        inputs.update(account=data["ACCOUNT"], positions=data["POSITION"], orders=data["ORDER"], deals=data["DEAL"])
        position = next(row for row in data["POSITION"] if row["symbol"] == inputs.get("symbol", "300319.SZ"))
        inputs["mark_price"] = number(position["mark_price"], positive=True)
    except (ValueError, TypeError, KeyError, StopIteration):
        blocked["blockers"] = ["snapshot_as_of_or_mark_unverified"]
        return dict(snapshot=report, state=blocked)
    try:
        state = rebuild_state(**inputs)
    except (ValueError, TypeError):
        blocked["blockers"] = ["snapshot_rebuild_template_invalid"]
        state = blocked
    return dict(snapshot=report, state=state)
