# -*- coding: utf-8 -*-
"""H82 回归（2026-11-13 立号）：startup 阶段卡死没有独立超时判据。

立号事实（11-13 实测，非估算）
------------------------------
- 07:55:04 判据 A 因"进程缺失"拉起；07:55:25 新实例起来后**卡在 startup**，
  心跳只写一次（`status=starting`、`last_success=None`），此后 25 分钟零日志。
- 直到 08:20:02 才由 HANG 判据（心跳静默 1200s）发现并 taskkill，**白等 1498s**。
- 代价：北京 13:00–13:24 无实时采集，网格 20 笔「买入(-1档)」被 `stale_quote` 拒掉。

本组用例锁死四件事
------------------
1. 只处理 `starting` + 当日 0 成功 + 超上限，不碰已在采数的进程（归 H62/H79）；
2. 未到上限、缺 started_at、时钟回拨、查不到进程 —— 一律不动作（失败关闭）；
3. 额度与 H60/H62 共享（`ZOMBIE_MAX_RESTARTS`），耗尽只告警不硬来；
4. 判据命中后 main 必须**本轮终止**，不再往下走判据 B / HANG。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import pytest

import watchdog as w

DAY = "2026-11-13"
START = datetime(2026, 11, 13, 7, 55, 25)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "LIVE_DIR", tmp_path)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "heartbeat.json")
    monkeypatch.setattr(w, "WATCHDOG_LOG", tmp_path / "logs" / "watchdog.log")
    monkeypatch.setattr(w, "STALL_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(w, "PROC_QUERY_TRUSTED", True)
    monkeypatch.setattr(w, "_sleep", lambda s: None)
    return tmp_path


def _hb(**over):
    base = {"date": DAY, "status": "starting",
            "started_at": START.isoformat(timespec="seconds"),
            "last_success_at": None,
            "updated_at": START.isoformat(timespec="seconds"),
            "collect_errors": 0}
    base.update(over)
    return base


PROCS = [(35276, "live_run.py")]


# ── 一、判据本身 ───────────────────────────────────────────────────────────

def test_startup_over_limit_triggers():
    need, why = w._startup_hang_decision(
        _hb(), PROCS, now=START + timedelta(seconds=610))
    assert need is True and "610s" in why and "0 成功" in why


def test_startup_under_limit_waits():
    need, why = w._startup_hang_decision(
        _hb(), PROCS, now=START + timedelta(seconds=590))
    assert need is False and "启动仅 590s" in why


def test_startup_exactly_at_limit_triggers():
    """边界取 >=：等于上限即判卡死（异常侧留余量，宁可早救）。"""
    need, _ = w._startup_hang_decision(
        _hb(), PROCS, now=START + timedelta(seconds=w.STALL_STARTUP_MAX))
    assert need is True


def test_running_status_not_handled():
    """已在跑（或已有成功）的进程归 H62/H79，本条不越界。"""
    need, why = w._startup_hang_decision(
        _hb(status="running"), PROCS, now=START + timedelta(seconds=900))
    assert need is False and "非 starting" in why


def test_with_success_record_not_handled():
    need, why = w._startup_hang_decision(
        _hb(last_success_at="2026-11-13T08:25:06"), PROCS,
        now=START + timedelta(seconds=1800))
    assert need is False and "归 H62/H79" in why


def test_no_process_not_handled():
    need, why = w._startup_hang_decision(
        _hb(), [], now=START + timedelta(seconds=900))
    assert need is False and "归判据 A" in why


def test_untrusted_proc_query_not_handled(monkeypatch):
    """查不到 ≠ 没有：进程查询不可信时必须失败关闭，不能说成"归判据 A"。"""
    monkeypatch.setattr(w, "PROC_QUERY_TRUSTED", False)
    need, why = w._startup_hang_decision(
        _hb(), [], now=START + timedelta(seconds=900))
    assert need is False and "不可信" in why


def test_missing_started_at_not_handled():
    need, why = w._startup_hang_decision(
        _hb(started_at=""), PROCS, now=START + timedelta(seconds=900))
    assert need is False and "缺少 started_at" in why


def test_future_started_at_not_handled():
    """时钟回拨/账本来自未来：不许靠"再等等"把额度挂住。"""
    need, why = w._startup_hang_decision(
        _hb(started_at="2026-11-13T09:00:00"), PROCS, now=START)
    assert need is False and "在未来" in why


# ── 二、动作与额度 ─────────────────────────────────────────────────────────

def _wire(monkeypatch, kill_ok=True, left_after=(), budget=0, uptime=610, closed=False):
    calls = {"kill": 0, "restart": 0, "alert": [], "bump": 0}
    monkeypatch.setattr(w, "_restart_budget", lambda day: budget)
    monkeypatch.setattr(w, "_bump_restart_budget",
                        lambda day, reason: calls.__setitem__("bump", calls["bump"] + 1))
    monkeypatch.setattr(w, "_today_watermark_reached_close", lambda: closed)

    def _kill(procs):
        calls["kill"] += 1
        return kill_ok

    def _restart(reason, wait=True):
        calls["restart"] += 1

    monkeypatch.setattr(w, "_kill_live_run", _kill)
    monkeypatch.setattr(w, "_live_run_processes", lambda: list(left_after))
    monkeypatch.setattr(w, "_restart", _restart)
    monkeypatch.setattr(w, "raise_alert",
                        lambda kind, msg, level=None, **k: calls["alert"].append(kind))
    monkeypatch.setattr(w, "_startup_hang_decision",
                        lambda hb, procs, now=None: (True, f"启动 {uptime}s 仍停在 starting、当日 0 成功"))
    return calls


def test_restart_kills_and_restarts(sandbox, monkeypatch):
    calls = _wire(monkeypatch)
    assert w._maybe_restart_startup(_hb(), PROCS) is True
    assert calls["kill"] == 1 and calls["restart"] == 1 and calls["bump"] == 1


def test_kill_failure_aborts_restart(sandbox, monkeypatch):
    """杀不掉就不能拉 —— 否则双实例写同一 CSV。"""
    calls = _wire(monkeypatch, kill_ok=False)
    assert w._maybe_restart_startup(_hb(), PROCS) is True
    assert calls["kill"] == 1 and calls["restart"] == 0


def test_residual_process_aborts_restart(sandbox, monkeypatch):
    calls = _wire(monkeypatch, left_after=[(35276, "live_run.py")])
    assert w._maybe_restart_startup(_hb(), PROCS) is True
    assert calls["restart"] == 0


def test_budget_exhausted_alerts_without_kill(sandbox, monkeypatch):
    calls = _wire(monkeypatch, budget=w.ZOMBIE_MAX_RESTARTS)
    assert w._maybe_restart_startup(_hb(), PROCS) is True
    assert calls["kill"] == 0 and calls["restart"] == 0
    assert calls["alert"] == [w.STARTUP_ALERT]


def test_dry_run_suppresses_action(sandbox, monkeypatch):
    calls = _wire(monkeypatch)
    assert w._maybe_restart_startup(_hb(), PROCS, dry=True) is True
    assert calls["kill"] == 0 and calls["restart"] == 0


def test_post_close_does_not_kill(sandbox, monkeypatch):
    """收盘后水位已满：手动启动做复盘/演练的实例不该在 600s 后被误杀。"""
    calls = _wire(monkeypatch, closed=True)
    assert w._maybe_restart_startup(_hb(), PROCS) is False
    assert calls["kill"] == 0 and calls["restart"] == 0


def test_not_triggered_lets_main_continue(sandbox, monkeypatch):
    """返回 False 才允许 main 继续走判据 B / HANG。"""
    calls = _wire(monkeypatch)
    monkeypatch.setattr(w, "_startup_hang_decision",
                        lambda hb, procs, now=None: (False, "启动仅 100s（< 600）"))
    assert w._maybe_restart_startup(_hb(), PROCS) is False
    assert calls["kill"] == 0


# ── 三、端到端：main 必须在本轮终止 ────────────────────────────────────────

def _write_hb(seconds_ago: int):
    started = datetime.now() - timedelta(seconds=seconds_ago)
    w.HEARTBEAT.write_text(json.dumps({
        "date": DAY, "status": "starting",
        "started_at": started.isoformat(timespec="seconds"),
        "last_success_at": None,
        "updated_at": started.isoformat(timespec="seconds"),
        "collect_errors": 0,
    }, ensure_ascii=False), encoding="utf-8")


def test_main_kills_startup_hang_within_one_round(sandbox, monkeypatch):
    """今天就是这条：08:20 才动手，改成 600s 后应在 08:05 前后就动手。"""
    _write_hb(610)
    calls = {"kill": 0, "restart": 0}
    state = {"procs": list(PROCS)}          # 真 kill 之后进程会消失，替身必须同步
    monkeypatch.setattr(w, "_live_run_processes", lambda: list(state["procs"]))

    def _kill(procs):
        calls["kill"] += 1
        state["procs"] = []
        return True

    monkeypatch.setattr(w, "_kill_live_run", _kill)
    monkeypatch.setattr(w, "_restart", lambda reason, wait=True: calls.__setitem__("restart", calls["restart"] + 1))
    monkeypatch.setattr(w, "raise_alert", lambda *a, **k: None)
    monkeypatch.setattr(w, "_restart_budget", lambda day: 1)
    monkeypatch.setattr(w, "_bump_restart_budget", lambda day, reason: 2)
    monkeypatch.setattr(w, "_today_watermark_reached_close", lambda: False)
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--ignore-window"])

    assert w.main() == 0
    assert calls["kill"] == 1 and calls["restart"] == 1


def test_main_waits_when_within_limit(sandbox, monkeypatch):
    _write_hb(200)
    calls = {"kill": 0}
    monkeypatch.setattr(w, "_live_run_processes", lambda: list(PROCS))
    monkeypatch.setattr(w, "_kill_live_run", lambda procs: calls.__setitem__("kill", calls["kill"] + 1) or True)
    monkeypatch.setattr(w, "_restart", lambda reason, wait=True: None)
    monkeypatch.setattr(w, "_report_success_staleness", lambda *a, **k: None)
    monkeypatch.setattr(w, "_maybe_restart_zombie", lambda *a, **k: False)
    monkeypatch.setattr(w, "_maybe_restart_stall", lambda *a, **k: False)
    monkeypatch.setattr(w, "_today_watermark_reached_close", lambda: False)
    monkeypatch.setattr(w, "_load_stall_ledger", lambda day: {})
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--ignore-window"])

    assert w.main() == 0
    assert calls["kill"] == 0
