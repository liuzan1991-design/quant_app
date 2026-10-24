# -*- coding: utf-8 -*-
"""H36 告警送达：`watchdog.log` 之外，人一定会看到的地方。

三条通道：桌面告警文件（清除时自动删）/ `ALERT_ACTIVE.json` / `alerts.jsonl` 审计流水。
外加两条红线：告警写盘失败**不得**影响主流程；恢复后必须**自动撤销**（避免"狼来了"）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import watchdog
from core import alerts


@pytest.fixture()
def paths(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(alerts, "LIVE_DIR", tmp_path / "live_outputs", raising=True)
    monkeypatch.setattr(alerts, "LOG_DIR", tmp_path / "live_outputs" / "logs", raising=True)
    monkeypatch.setattr(alerts, "ACTIVE_FILE", tmp_path / "ALERT_ACTIVE.json", raising=True)
    monkeypatch.setattr(alerts, "JSONL_FILE", tmp_path / "logs" / "alerts.jsonl", raising=True)
    monkeypatch.setattr(alerts, "DESKTOP_FILE", tmp_path / "Desktop" / "量化系统告警.txt", raising=True)
    monkeypatch.delenv(alerts.ENV_DESKTOP, raising=False)
    return tmp_path


def test_h36_raise_writes_three_channels(paths: Path) -> None:
    got = alerts.raise_alert("collect_stale", "采集停摆 901s")
    assert got and got["message"] == "采集停摆 901s"
    active = json.loads((paths / "ALERT_ACTIVE.json").read_text(encoding="utf-8"))
    assert "collect_stale" in active
    desktop = (paths / "Desktop" / "量化系统告警.txt").read_text(encoding="utf-8")
    assert "collect_stale" in desktop and "901s" in desktop
    lines = (paths / "logs" / "alerts.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1 and json.loads(lines[0])["kind"] == "collect_stale"


def test_h36_repeat_keeps_since_updates_last_seen(paths: Path) -> None:
    t0 = datetime(2026, 10, 24, 9, 0, 0)
    alerts.raise_alert("collect_stale", "第一次", now=t0)
    alerts.raise_alert("collect_stale", "第二次", now=t0 + timedelta(minutes=5))
    item = alerts.active_alerts()["collect_stale"]
    assert item["since"] == "2026-10-24 09:00:00", "since 必须保留首次时间"
    assert item["last_seen"] == "2026-10-24 09:05:00"
    assert item["message"] == "第二次"
    lines = (paths / "logs" / "alerts.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2, "审计流水每次都要留痕"


def test_h36_clear_removes_desktop_file(paths: Path) -> None:
    alerts.raise_alert("collect_stale", "x")
    assert (paths / "Desktop" / "量化系统告警.txt").exists()
    alerts.clear_alert("collect_stale")
    assert alerts.active_alerts() == {}
    assert not (paths / "Desktop" / "量化系统告警.txt").exists(), "全清后桌面文件必须删除"


def test_h36_clear_one_leaves_others(paths: Path) -> None:
    alerts.raise_alert("collect_stale", "A")
    alerts.raise_alert("disk_full", "B")
    alerts.clear_alert("collect_stale")
    assert set(alerts.active_alerts()) == {"disk_full"}
    desktop = (paths / "Desktop" / "量化系统告警.txt").read_text(encoding="utf-8")
    assert "disk_full" in desktop and "collect_stale" not in desktop


def test_h36_desktop_can_be_disabled(paths: Path, monkeypatch) -> None:
    monkeypatch.setenv(alerts.ENV_DESKTOP, "0")
    alerts.raise_alert("collect_stale", "x")
    assert alerts.active_alerts(), "状态文件仍要写"
    assert not (paths / "Desktop" / "量化系统告警.txt").exists(), "关掉后不得写桌面"


def test_h36_failure_never_breaks_caller(paths: Path, monkeypatch) -> None:
    """告警写盘失败 ⇒ 返回 None，绝不抛（看门狗不能因为告警坏了而崩）。"""
    monkeypatch.setattr(alerts, "_save", lambda active: (_ for _ in ()).throw(OSError("磁盘满")))
    assert alerts.raise_alert("collect_stale", "x") is None
    alerts.clear_alert("collect_stale")   # 不抛即通过


def test_h36_no_desktop_still_delivers_other_channels(paths: Path, monkeypatch) -> None:
    """找不到桌面目录 ⇒ 桌面通道跳过，状态文件 + 审计流水仍要写。"""
    monkeypatch.setattr(alerts, "DESKTOP_FILE", None)
    assert alerts.raise_alert("collect_stale", "x") is not None
    assert alerts.active_alerts(), "状态文件必须写"
    assert (paths / "logs" / "alerts.jsonl").exists()


def test_h36_desktop_resolution_is_real() -> None:
    """解析出来的桌面目录必须真实存在（否则又是一个"没人看到"的落点）。

    本机注册表把桌面重定向到 D:\\Desktop；写死 `~/Desktop` 会静默失效。
    """
    d = alerts.resolve_desktop_dir()
    if d is not None:
        assert d.is_dir(), f"解析到的桌面不存在: {d}"
        assert alerts.DESKTOP_FILE is not None and alerts.DESKTOP_FILE.parent == d


# ── 看门狗接线：告警真被送达、恢复真被撤销 ──
def test_h36_watchdog_raises_on_stale(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(watchdog, "raise_alert", lambda k, m: calls.append((k, m)))
    hb = {"status": "running", "started_at": "2026-10-24 04:30:00",
          "last_success_at": "2026-10-24 07:43:26", "collect_errors": 7}
    watchdog._report_success_staleness(hb, succ_age=1800, suffix="")
    assert calls and calls[0][0] == "collect_stale"


def test_h36_watchdog_clears_on_recovery(monkeypatch) -> None:
    cleared = []
    monkeypatch.setattr(watchdog, "clear_alert", lambda k: cleared.append(k))
    hb = {"status": "running", "last_success_at": "2026-10-24 08:00:00", "collect_errors": 0}
    watchdog._report_success_staleness(hb, succ_age=60, suffix="")
    assert cleared == ["collect_stale"]


def test_h36_watchdog_raises_when_never_succeeded(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(watchdog, "raise_alert", lambda k, m: calls.append((k, m)))
    hb = {"status": "running", "started_at": "2026-10-24 04:30:00", "collect_errors": 3}
    watchdog._report_success_staleness(hb, succ_age=None, suffix="")
    assert calls and "从未采集成功" in calls[0][1]
