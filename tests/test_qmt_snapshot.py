"""Offline evidence validation; all data/timestamps are synthetic."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from core.qmt_snapshot import TABLES, collect_consistent_snapshot, rebuild_from_snapshot, validate_snapshot
from test_qmt_state_rebuild import snapshot

START = datetime.fromisoformat("2026-11-06T10:00:00+08:00")


@pytest.fixture
def bundle():
    payload = {"ACCOUNT": {"available": 500000.0, "total_equity": 500000.0},
               "POSITION": [dict(symbol="300319.SZ", quantity=200, sellable_quantity=200, mark_price=15.0)],
               "ORDER": [], "DEAL": []}
    cycles = []
    for cycle in range(2):
        reads = []
        for index, table in enumerate(TABLES):
            step = cycle * 4 + index
            reads.append(dict(table=table, account_scope="synthetic-account", complete=True,
                              started_at=(START + timedelta(seconds=step / 10)).isoformat(),
                              finished_at=(START + timedelta(seconds=step / 10 + .05)).isoformat(),
                              data=deepcopy(payload[table])))
        cycles.append(dict(reads=reads))
    return dict(account_scope="synthetic-account", cycles=cycles)


def read(bundle, cycle, table):
    return bundle["cycles"][cycle]["reads"][TABLES.index(table)]


def now(bundle, extra=.1):
    end = bundle["cycles"][-1]["reads"][-1]["finished_at"]
    return (datetime.fromisoformat(end) + timedelta(seconds=extra)).isoformat()


def template(snapshot, bundle):
    result = deepcopy(snapshot)
    as_of = validate_snapshot(bundle, now=now(bundle))["as_of"]
    result["as_of"] = result["coverage"]["to"] = result["risk"]["as_of"] = as_of
    return result


def test_stable_double_read_keeps_actual_windows_and_is_not_called_atomic(bundle):
    original = deepcopy(bundle)
    report = validate_snapshot(bundle, now=now(bundle))
    assert report["valid"] and report["atomic"] is False
    assert report["consistency_model"] == "stable_double_read"
    assert len(report["read_windows"]) == 8 and report["span_seconds"] == .75
    assert len({r["finished_at"] for r in report["read_windows"]}) == 8
    assert report["as_of"] == bundle["cycles"][1]["reads"][-1]["finished_at"]
    assert original == bundle
    report["data"]["POSITION"][0]["quantity"] = 1
    assert bundle == original


@pytest.mark.parametrize("table", TABLES)
def test_changed_table_is_rejected_even_with_a_shared_logical_label(bundle, table):
    row = read(bundle, 1, table)
    if table == "ACCOUNT":
        row["data"]["available"] -= 1
    elif table == "POSITION":
        row["data"][0]["quantity"] += 100
    else:
        row["data"].append({"order_id" if table == "ORDER" else "trade_id": "new-id",
                            "time": "2026-11-06T09:59:00+08:00"})
    bundle["as_of"] = now(bundle)
    report = validate_snapshot(bundle, now=now(bundle))
    assert not report["valid"] and "changed_" + table.lower() in report["blockers"]
    assert report["data"] is None and report["as_of"] is None


def test_order_of_rows_does_not_create_false_instability(bundle):
    extra = dict(symbol="600000.SH", quantity=1, sellable_quantity=1, mark_price=10)
    read(bundle, 0, "POSITION")["data"].append(extra)
    read(bundle, 1, "POSITION")["data"].insert(0, deepcopy(extra))
    assert validate_snapshot(bundle, now=now(bundle))["valid"]


@pytest.mark.parametrize("table", ["ORDER", "DEAL"])
def test_equal_end_positions_cannot_hide_events_during_collection(bundle, table):
    for cycle in range(2):
        read(bundle, cycle, table)["data"] = [{"order_id" if table == "ORDER" else "trade_id": "event",
                                              "time": "2026-11-06T10:00:00.100000+08:00"}]
    report = validate_snapshot(bundle, now=now(bundle))
    assert not report["valid"] and "event_during_collection" in report["blockers"]


def test_future_event_is_rejected(bundle):
    for cycle in range(2):
        read(bundle, cycle, "DEAL")["data"] = [dict(trade_id="future", time="2026-11-06T10:01:00+08:00")]
    assert "future_deal_event" in validate_snapshot(bundle, now=now(bundle))["blockers"]


def test_cross_account_table_is_rejected(bundle):
    read(bundle, 1, "DEAL")["account_scope"] = "different-account"
    assert validate_snapshot(bundle, now=now(bundle))["blockers"] == ["account_scope_mismatch"]


@pytest.mark.parametrize("complete", [False, None, 1])
def test_incomplete_or_uncertain_empty_table_does_not_prove_no_activity(bundle, complete):
    read(bundle, 0, "ORDER")["complete"] = complete
    assert validate_snapshot(bundle, now=now(bundle))["blockers"] == ["table_read_incomplete"]


def test_a_single_old_native_pass_is_not_upgraded_to_a_stable_snapshot(bundle):
    bundle["cycles"] = bundle["cycles"][:1]
    assert validate_snapshot(bundle, now=now(bundle))["blockers"] == ["two_cycles_required"]


def test_stale_read_is_not_retimed_to_now(bundle):
    assert validate_snapshot(bundle, now=now(bundle, 6))["blockers"] == ["stale_snapshot"]


def test_excessive_duration_is_rejected(bundle):
    assert validate_snapshot(bundle, now=now(bundle), max_span_seconds=.5)["blockers"] == ["snapshot_span_exceeded_or_zero"]


def test_read_clock_must_be_aware_and_sequential(bundle):
    read(bundle, 0, "ACCOUNT")["started_at"] = "2026-11-06T10:00:00"
    assert validate_snapshot(bundle, now=now(bundle))["blockers"] == ["timezone_required"]


def test_overlapping_or_reversed_reads_are_rejected(bundle):
    read(bundle, 1, "ACCOUNT")["started_at"] = START.isoformat()
    assert validate_snapshot(bundle, now=now(bundle))["blockers"] == ["read_windows_not_sequential"]


def test_reads_in_the_future_are_rejected(bundle):
    assert validate_snapshot(bundle, now=START.isoformat())["blockers"] == ["future_snapshot"]


def test_cross_midnight_collection_is_rejected(bundle):
    shift = timedelta(hours=13, minutes=59, seconds=59.5)
    for cycle in bundle["cycles"]:
        for row in cycle["reads"]:
            for field in ("started_at", "finished_at"):
                row[field] = (datetime.fromisoformat(row[field]) + shift).isoformat()
    assert validate_snapshot(bundle, now=now(bundle))["blockers"] == ["snapshot_crosses_trade_date"]


@pytest.mark.parametrize("table", ["POSITION", "ORDER", "DEAL"])
def test_duplicate_table_identities_are_not_silently_collapsed(bundle, table):
    row = read(bundle, 0, table)
    if table != "POSITION":
        row["data"] = [{"order_id" if table == "ORDER" else "trade_id": "duplicate",
                         "time": "2026-11-06T09:30:00+08:00"}]
    row["data"].append(deepcopy(row["data"][0]))
    assert "missing_or_duplicate_identity" in validate_snapshot(bundle, now=now(bundle))["blockers"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_actual_available_cash_rejects_the_snapshot(bundle, value):
    read(bundle, 0, "ACCOUNT")["data"]["available"] = value
    assert not validate_snapshot(bundle, now=now(bundle))["valid"]


def test_retry_recollects_all_tables_and_never_accepts_the_failed_attempt(bundle):
    tick = 0
    calls = []
    def clock():
        nonlocal tick
        tick += 1
        return (START + timedelta(seconds=tick * .02)).isoformat()
    def adapter(table):
        index = len(calls)
        calls.append(table)
        value = deepcopy(read(bundle, 0, table)["data"])
        if index == 4:
            value["available"] -= 1
        return dict(account_scope="synthetic-account", complete=True, data=value)
    report = collect_consistent_snapshot(adapter, account_scope="synthetic-account", clock=clock)
    assert report["report"]["valid"] and len(report["attempts"]) == 2
    assert report["attempts"][0]["report"]["blockers"] == ["changed_account"]
    assert calls == list(TABLES) * 4


def test_adapter_errors_are_bounded_and_reported():
    calls = []
    def adapter(table):
        calls.append(table)
        raise OSError("synthetic failure")
    result = collect_consistent_snapshot(adapter, account_scope="synthetic-account", max_attempts=2)
    assert len(calls) == len(result["attempts"]) == 2
    assert result["report"]["blockers"] == ["read_adapter_error"]


@pytest.mark.parametrize("attempts", [0, 11, True, 1.5])
def test_retry_budget_is_validated_before_touching_adapter(attempts):
    def adapter(table):
        pytest.fail("Adapter should not run")
    with pytest.raises(ValueError):
        collect_consistent_snapshot(adapter, account_scope="synthetic-account", max_attempts=attempts)


def test_snapshot_gate_uses_read_quote_without_overwriting_ownership_or_risk(snapshot, bundle):
    inputs = template(snapshot, bundle)
    original = deepcopy(inputs)
    result = rebuild_from_snapshot(bundle, inputs, now=now(bundle))
    assert result["state"]["mode"] == "ready" and inputs == original
    inputs["anchor"]["verified"] = False
    assert rebuild_from_snapshot(bundle, inputs, now=now(bundle))["state"]["blockers"] == ["anchor_unverified"]


def test_invalid_snapshot_blocks_the_new_rebuild_pipeline(snapshot, bundle):
    inputs = template(snapshot, bundle)
    read(bundle, 1, "POSITION")["data"][0]["quantity"] += 100
    result = rebuild_from_snapshot(bundle, inputs, now=now(bundle))
    assert result["state"]["mode"] == "halt" and not result["state"]["can_open"]


def test_stale_template_timestamp_is_not_forcibly_rewritten(snapshot, bundle):
    assert rebuild_from_snapshot(bundle, snapshot, now=now(bundle))["state"]["blockers"] == ["snapshot_as_of_or_mark_unverified"]


def test_same_number_of_missing_quote_rows_cannot_authorize_a_rebuild(snapshot, bundle):
    inputs = template(snapshot, bundle)
    for cycle in range(2):
        read(bundle, cycle, "POSITION")["data"][0].pop("mark_price")
    assert rebuild_from_snapshot(bundle, inputs, now=now(bundle))["state"]["mode"] == "halt"
