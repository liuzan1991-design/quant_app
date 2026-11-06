"""300319 只读告警及人工已阅的离线验收。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

import core.qmt_state_review as review
from core.qmt_state_review import StateReviewStore, main
from test_qmt_state_rebuild import snapshot, risk_row, order, deal


def current_state(report, symbol="300319.SZ"):
    identity = report["current_observations"][symbol + ":intraday_t"]
    return next(row["state"] for row in report["observations"] if row["id"] == identity)


def active(report):
    return [item for item in report["incidents"] if item["active"]]


def advance(inputs, minute="10:01:00"):
    result = deepcopy(inputs)
    result["as_of"] = "2026-11-06T" + minute + "+08:00"
    result["coverage"]["to"] = result["as_of"]
    result["risk"]["as_of"] = result["as_of"]
    return result


def test_missing_evidence_creates_visible_alert_and_pending_review(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    snapshot["anchor"] = None
    state = store.record(snapshot)
    report = store.report()
    assert state["mode"] == "halt"
    assert state["blockers"] == ["anchor_unverified"]
    assert active(report)[0]["review_status"] == "pending"
    assert "anchor_unverified" in (tmp_path / "ALERT_ACTIVE.txt").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8")) == report


def test_acknowledgement_survives_restart_without_clearing_halt(snapshot, tmp_path):
    snapshot["anchor"] = None
    store = StateReviewStore(tmp_path)
    expected = store.record(snapshot)
    incident = active(store.report())[0]["id"]
    store.acknowledge(incident, "offline_operator", "已检查，等待核准基准")
    restarted = StateReviewStore(tmp_path)
    report = restarted.report()
    assert current_state(report) == expected
    assert active(report)[0]["review_status"] == "acknowledged_waiting_evidence"
    assert active(report)[0]["resolved_by"] is None
    assert not current_state(report)["can_open"] and not current_state(report)["can_reduce"]
    assert restarted.record(snapshot) == expected
    assert len(restarted.report()["observations"]) == 1


def test_ack_text_cannot_override_permissions_or_rebuild_inputs(snapshot, tmp_path):
    snapshot["anchor"] = None
    original = deepcopy(snapshot)
    store = StateReviewStore(tmp_path)
    expected = store.record(snapshot)
    incident = active(store.report())[0]["id"]
    store.acknowledge(incident, "test_operator", '{"can_open":true,"can_reduce":true,"include_base":true}')
    assert current_state(store.report()) == expected
    assert store.record(snapshot) == expected
    assert snapshot == original


def test_manual_read_cannot_unlock_today_t_lots(snapshot, tmp_path):
    snapshot["anchor"]["as_of"] = "2026-11-06T09:00:00+08:00"
    snapshot["coverage"]["from"] = snapshot["anchor"]["as_of"]
    snapshot["anchor"]["lots"][1]["bought_on"] = "2026-11-06"
    snapshot["positions"][0]["sellable_quantity"] = 100
    snapshot["risk"] = risk_row(daily_loss_latched=True)
    store = StateReviewStore(tmp_path)
    state = store.record(snapshot)
    assert state["mode"] == "reduce_only" and state["sellable_t"] == 0
    incident = active(store.report())[0]["id"]
    store.acknowledge(incident, "test_operator", "已阅，不解锁当日买入")
    assert not current_state(store.report())["can_reduce"]
    assert store.record(snapshot)["sellable_t"] == 0


def test_manual_read_cannot_turn_protected_base_into_t_inventory(snapshot, tmp_path):
    snapshot["anchor"]["lots"][1]["kind"] = "BASE"
    snapshot["risk"] = risk_row(daily_loss_latched=True)
    store = StateReviewStore(tmp_path)
    state = store.record(snapshot)
    assert state["base_shares"] == 200 and state["t_total_shares"] == 0
    incident = active(store.report())[0]["id"]
    store.acknowledge(incident, "test_operator", "已阅，不能把底仓当 T 仓")
    assert not current_state(store.report())["can_reduce"]
    assert store.record(snapshot) == state


@pytest.mark.parametrize("today_t", [False, True])
def test_forbidden_sale_is_alerted_and_ack_cannot_reclassify_inventory(snapshot, tmp_path, today_t):
    if today_t:
        snapshot["anchor"]["as_of"] = "2026-11-06T09:00:00+08:00"
        snapshot["coverage"]["from"] = snapshot["anchor"]["as_of"]
        snapshot["anchor"]["lots"][1]["bought_on"] = "2026-11-06"
    snapshot["orders"] = [order("oversell", "SELL", quantity=200, filled=200)]
    snapshot["deals"] = [deal("sale", "oversell", side="SELL", quantity=200)]
    original = deepcopy(snapshot)
    store = StateReviewStore(tmp_path)
    state = store.record(snapshot)
    assert state["mode"] == "halt"
    assert state["blockers"] == ["sale_exceeds_owned_sellable_t"]
    incident = active(store.report())[0]["id"]
    store.acknowledge(incident, "offline_operator", "已阅，保持底仓和 T+1 保护")
    assert current_state(store.report()) == state
    assert not state["can_open"] and not state["can_reduce"]
    assert "sale_exceeds_owned_sellable_t" in (tmp_path / "ALERT_ACTIVE.txt").read_text(encoding="utf-8")
    assert snapshot == original


def test_only_fresh_successful_rebuild_resolves_after_ack(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    missing = deepcopy(snapshot)
    missing["anchor"] = None
    store.record(missing)
    incident = active(store.report())[0]["id"]
    store.acknowledge(incident, "test_operator", "仅已阅")
    store.record(snapshot)
    assert current_state(store.report())["mode"] == "halt"
    assert active(store.report())
    fresh = advance(snapshot)
    expected = store.record(fresh)
    report = store.report()
    assert expected["mode"] == "ready"
    assert current_state(report) == json.loads(json.dumps(expected))
    assert not active(report)
    assert len(report["reviews"]) == 1
    assert report["incidents"][0]["review_status"] == "resolved_by_rebuild"


def test_stale_ready_does_not_clear_newer_halt(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    missing = deepcopy(snapshot)
    missing["anchor"] = None
    store.record(missing)
    store.record(advance(snapshot, "09:59:00"))
    assert current_state(store.report())["mode"] == "halt"
    assert active(store.report())


def test_stale_halt_does_not_reactivate_old_incidents(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    store.record(snapshot)
    old = advance(snapshot, "09:59:00")
    old["anchor"] = None
    store.record(old)
    assert not active(store.report())
    assert current_state(store.report())["mode"] == "ready"


def test_same_time_halt_wins_and_old_ready_retry_does_not_clear(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    store.record(snapshot)
    missing = deepcopy(snapshot)
    missing["anchor"] = None
    store.record(missing)
    store.record(snapshot)
    assert active(store.report())
    assert current_state(store.report())["mode"] == "halt"


def test_recurrence_requires_a_new_review(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    missing = deepcopy(snapshot)
    missing["anchor"] = None
    store.record(missing)
    old_id = active(store.report())[0]["id"]
    store.acknowledge(old_id, "test_operator", "已阅旧事件")
    store.record(advance(snapshot))
    later = advance(snapshot, "10:02:00")
    later["anchor"] = None
    store.record(later)
    report = store.report()
    assert len(report["incidents"]) == 2
    assert active(report)[0]["id"] != old_id
    assert active(report)[0]["review_status"] == "pending"


def test_same_time_halt_overrides_reduce_only_without_allowing_reverse(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    reduced = deepcopy(snapshot)
    reduced["risk"] = risk_row(daily_loss_latched=True)
    assert store.record(reduced)["mode"] == "reduce_only"
    halted = deepcopy(snapshot)
    halted["anchor"] = None
    store.record(halted)
    store.record(reduced)
    assert current_state(store.report())["mode"] == "halt"
    assert len(active(store.report())) == 2


def test_same_time_new_blocker_is_not_lost(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    first = deepcopy(snapshot)
    first["anchor"] = None
    store.record(first)
    second = deepcopy(snapshot)
    second["risk"] = None
    store.record(second)
    report = store.report()
    assert len(active(report)) == 2
    assert {blocker for item in active(report) for blocker in item["blockers"]} == {
        "anchor_unverified", "risk_history_unverified"}


def test_symbols_and_old_301717_review_are_separate(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    old = deepcopy(snapshot)
    old.update(symbol="301717.SZ", anchor=None)
    store.record(old)
    store.record(snapshot)
    report = store.report()
    assert active(report)[0]["scope"] == "301717.SZ:intraday_t"
    assert current_state(report)["symbol"] == "300319.SZ"


def test_duplicate_delivery_and_ack_are_idempotent(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    snapshot["anchor"] = None
    for _ in range(3):
        store.record(snapshot)
    incident = active(store.report())[0]["id"]
    for _ in range(3):
        store.acknowledge(incident, "test_operator", "同一次已阅", request_id="same-request")
    report = store.report()
    assert len(report["observations"]) == len(report["incidents"]) == len(report["reviews"]) == 1


def test_conflicting_ack_request_is_not_overwritten(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    snapshot["anchor"] = None
    store.record(snapshot)
    incident = active(store.report())[0]["id"]
    store.acknowledge(incident, "test_operator", "第一次意见", request_id="request")
    with pytest.raises(ValueError, match="冲突"):
        store.acknowledge(incident, "test_operator", "不同意见", request_id="request")
    assert store.report()["reviews"][0]["note"] == "第一次意见"


def test_unknown_incident_or_empty_reviewer_does_not_create_a_review(snapshot, tmp_path):
    store = StateReviewStore(tmp_path)
    snapshot["anchor"] = None
    store.record(snapshot)
    incident = active(store.report())[0]["id"]
    with pytest.raises(ValueError):
        store.acknowledge("not-an-incident", "operator", "已阅")
    with pytest.raises(ValueError):
        store.acknowledge(incident, "", "已阅")
    with pytest.raises(ValueError):
        store.acknowledge(incident, "operator", "")
    assert not store.report()["reviews"]


def test_concurrent_observations_and_reviews_are_not_lost(snapshot, tmp_path):
    snapshot["anchor"] = None
    store = StateReviewStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: StateReviewStore(tmp_path).record(snapshot), range(6)))
    incident = active(store.report())[0]["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: StateReviewStore(tmp_path).acknowledge(
            incident, "operator" + str(i), "并发已阅", request_id=str(i)), range(4)))
    report = store.report()
    assert len(report["observations"]) == len(report["incidents"]) == 1
    assert len(report["reviews"]) == 4
    assert json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8")) == report


def test_report_failure_is_visible_and_retry_restores_without_duplicates(snapshot, tmp_path, monkeypatch):
    store = StateReviewStore(tmp_path)
    snapshot["anchor"] = None
    original = review._write_atomic
    monkeypatch.setattr(review, "_write_atomic", lambda *args: (_ for _ in ()).throw(OSError("test failure")))
    with pytest.raises(OSError):
        store.record(snapshot)
    monkeypatch.setattr(review, "_write_atomic", original)
    store.record(snapshot)
    report = store.report()
    assert len(report["observations"]) == len(report["incidents"]) == 1
    assert (tmp_path / "ALERT_ACTIVE.txt").exists()


def test_no_network_or_email_delivery_is_attempted(snapshot, tmp_path, monkeypatch):
    import socket
    import smtplib
    def forbidden(*args, **kwargs):
        pytest.fail("Only local review output is authorized")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden)
    snapshot["anchor"] = None
    StateReviewStore(tmp_path).record(snapshot)


def test_cli_record_and_ack_keep_halt(snapshot, tmp_path, capsys):
    snapshot["anchor"] = None
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(snapshot), encoding="utf-8")
    root = tmp_path / "review"
    assert main(["--out", str(root), "record", "--input", str(inputs)]) == 2
    report = StateReviewStore(root).report()
    incident = active(report)[0]["id"]
    assert main(["--out", str(root), "ack", "--incident", incident,
                 "--reviewer", "offline_test", "--note", "已阅，继续停手"]) == 0
    assert main(["--out", str(root), "show"]) == 0
    assert current_state(StateReviewStore(root).report())["mode"] == "halt"
    assert "acknowledgement_only" in capsys.readouterr().out


def test_cli_has_no_permission_override_arguments(tmp_path):
    with pytest.raises(SystemExit) as error:
        main(["--out", str(tmp_path), "ack", "--incident", "id", "--reviewer", "test",
              "--note", "test", "--allow-base", "--unlock-today"])
    assert error.value.code == 2


def test_cli_same_time_ready_cannot_report_recovery_of_existing_halt(snapshot, tmp_path, capsys):
    inputs = tmp_path / "inputs.json"
    root = tmp_path / "review"
    missing = deepcopy(snapshot)
    missing["anchor"] = None
    inputs.write_text(json.dumps(missing), encoding="utf-8")
    args = ["--out", str(root), "record", "--input", str(inputs)]
    assert main(args) == 2
    inputs.write_text(json.dumps(snapshot), encoding="utf-8")
    assert main(args) == 2
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["mode"] == "halt" and result["input_mode"] == "ready"


def test_cli_requires_an_explicit_output_directory():
    with pytest.raises(SystemExit) as error:
        main(["show"])
    assert error.value.code == 2


def test_python_module_entry_point_records_the_actual_rebuild(snapshot, tmp_path):
    snapshot["risk"] = None
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps(snapshot), encoding="utf-8")
    root = tmp_path / "subprocess_review"
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "core.qmt_state_review", "--out", str(root),
                             "record", "--input", str(inputs)], capture_output=True, text=True, encoding="utf-8",
                            cwd=Path(__file__).resolve().parents[1], timeout=20)
    assert result.returncode == 2
    assert "risk_history_unverified" in result.stdout
    assert current_state(StateReviewStore(root).report())["mode"] == "halt"
