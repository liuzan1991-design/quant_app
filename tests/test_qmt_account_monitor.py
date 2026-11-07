"""Local warning diagnostics must have no effect on strategy permissions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

import core.qmt_account_monitor as monitor
from core.qmt_account_monitor import account_exposure, main, monitor_bundle, publish_exposure
from core.qmt_snapshot import rebuild_from_snapshot, validate_snapshot
from test_qmt_snapshot import bundle, now, read, template
from test_qmt_state_rebuild import snapshot


def set_value(bundle, *, quantity, price, equity=500000.0):
    for cycle in range(2):
        row = read(bundle, cycle, "POSITION")["data"][0]
        row.update(quantity=quantity, sellable_quantity=quantity, mark_price=price)
        read(bundle, cycle, "ACCOUNT")["data"]["total_equity"] = equity


def test_two_denominators_have_separate_meaning_and_alerts(bundle):
    set_value(bundle, quantity=10000, price=25)
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert report["account_symbol_value"] == 250000
    assert report["account_equity_fraction"] == .5
    assert report["account_value_vs_strategy_reference"] == pytest.approx(250000 / 300000)
    assert report["alerts"] == ["ACCOUNT_VALUE_VS_STRATEGY_REFERENCE_HIGH"]
    assert report["affects_strategy_permissions"] is report["strategy_cap_changed"] is False


def test_account_warning_is_not_mislabeled_as_a_strategy_position_ratio(bundle):
    set_value(bundle, quantity=10000, price=40)
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert report["account_equity_fraction"] == .8
    assert report["account_value_vs_strategy_reference"] == pytest.approx(4 / 3)
    assert set(report["alerts"]) == {"ACCOUNT_SYMBOL_CONCENTRATION_HIGH", "ACCOUNT_VALUE_VS_STRATEGY_REFERENCE_HIGH"}


def test_threshold_is_inclusive_and_does_not_change_strategy_cap(bundle):
    set_value(bundle, quantity=10000, price=21, equity=300000)
    result = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert result["status"] == "warning" and len(result["alerts"]) == 2


def test_below_threshold_is_a_successful_observation(bundle):
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert report["status"] == "ok" and not report["alerts"]


@pytest.mark.parametrize("equity", [None, 0, -1, True])
def test_unknown_account_equity_never_reports_a_cleared_concentration(bundle, equity):
    set_value(bundle, quantity=10000, price=25, equity=equity)
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert report["status"] == "data_unavailable" and report["account_equity_fraction"] is None
    assert report["account_value_vs_strategy_reference"] == pytest.approx(250000 / 300000)
    assert "ACCOUNT_EQUITY_UNAVAILABLE_NOT_CLEARED" in report["alerts"]


@pytest.mark.parametrize("price", [None, 0, -1])
def test_missing_or_invalid_positive_position_quote_stays_unknown(bundle, price):
    set_value(bundle, quantity=100, price=price)
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert report["status"] == "data_unavailable" and report["account_symbol_value"] is None


def test_zero_inventory_does_not_require_an_artificial_quote(bundle):
    set_value(bundle, quantity=0, price=None)
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert report["status"] == "ok" and report["account_symbol_value"] == 0


def test_complete_position_absence_can_establish_zero_exposure(bundle):
    for cycle in range(2):
        read(bundle, cycle, "POSITION")["data"] = []
    assert monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))["account_symbol_value"] == 0
    read(bundle, 0, "POSITION")["complete"] = False
    assert monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))["status"] == "data_unavailable"


def test_invalid_or_stale_snapshot_is_an_unknown_warning_not_a_clearance(bundle):
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle, 8))
    assert report["alerts"] == ["SNAPSHOT_INVALID_DATA_NOT_CLEARED"]
    assert report["account_equity_fraction"] is None


def test_local_warning_output_is_atomic_and_separate_from_review_ledger(bundle, tmp_path):
    original = deepcopy(bundle)
    root = tmp_path / "independent"
    report = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle), output_dir=root)
    assert json.loads((root / "account_exposure.json").read_text(encoding="utf-8")) == report
    text = (root / "ACCOUNT_EXPOSURE_READONLY.txt").read_text(encoding="utf-8")
    assert "账户总权益" in text and "策略30万元本金" in text and "不是策略实际仓位占比" in text
    assert "分母一，账户总权益：500000.00元" in text
    assert "分母二，策略本金参考：300000.00元" in text
    assert sorted(p.name for p in root.iterdir()) == ["ACCOUNT_EXPOSURE_READONLY.txt", "account_exposure.json"]
    assert bundle == original


def test_warning_and_output_failure_do_not_mutate_strategy_state(snapshot, bundle, tmp_path, monkeypatch):
    # Consistent synthetic account: cash210000 + stock705000 = equity915000.
    set_value(bundle, quantity=47000, price=15, equity=915000)
    for cycle in range(2):
        read(bundle, cycle, "ACCOUNT")["data"]["available"] = 210000
    inputs = template(snapshot, bundle)
    inputs["anchor"]["external_quantity"] = 46800
    before = deepcopy(inputs)
    state = rebuild_from_snapshot(bundle, inputs, now=now(bundle))["state"]
    assert state["can_open"] and state["symbol_cap_scope"] == "strategy_owned"
    warning = monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))
    assert warning["status"] == "warning"
    assert rebuild_from_snapshot(bundle, inputs, now=now(bundle))["state"] == state
    monkeypatch.setattr(monitor, "_write_atomic", lambda *args: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        publish_exposure(warning, tmp_path)
    assert inputs == before and rebuild_from_snapshot(bundle, inputs, now=now(bundle))["state"] == state


def test_monitor_attempts_no_network_email_orders_or_review_ack(bundle, monkeypatch):
    import socket
    import smtplib
    from core.qmt_state_review import StateReviewStore
    def forbidden(*args, **kwargs):
        pytest.fail("Independent diagnostic may not transmit or change permissions")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(smtplib, "SMTP", forbidden)
    monkeypatch.setattr(StateReviewStore, "acknowledge", forbidden)
    assert monitor_bundle(bundle, symbol="300319.SZ", now=now(bundle))["status"] == "ok"


def test_cli_requires_explicit_input_and_output_directories():
    with pytest.raises(SystemExit):
        main([])


def test_cli_old_single_pass_returns_unknown_and_does_not_report_freshness(tmp_path):
    path = tmp_path / "single.json"
    path.write_text(json.dumps({"records": []}), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["--input", str(path), "--out", str(out)]) == 1
    assert json.loads((out / "account_exposure.json").read_text(encoding="utf-8"))["status"] == "data_unavailable"


def test_python_module_cli_writes_only_requested_diagnostics(bundle, tmp_path):
    # Shift synthetic clocks to just before this subprocess test.
    first = datetime.fromisoformat(bundle["cycles"][0]["reads"][0]["started_at"])
    shift = datetime.now(timezone.utc) - timedelta(seconds=1) - first
    for cycle in bundle["cycles"]:
        for row in cycle["reads"]:
            for key in ("started_at", "finished_at"):
                row[key] = (datetime.fromisoformat(row[key]) + shift).isoformat()
    path = tmp_path / "input.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", "core.qmt_account_monitor",
                             "--input", str(path), "--out", str(tmp_path / "report")],
                            cwd=Path(__file__).resolve().parents[1], text=True, encoding="utf-8",
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["affects_strategy_permissions"] is False
