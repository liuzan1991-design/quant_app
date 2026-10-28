# -*- coding: utf-8 -*-
"""H62 回归：心跳静默但**进程还活着**（悬挂）⇒ taskkill + 重启。

背景（2026-10-28 实证）：04:30 启动，5 次 kline_empty 后进程在 04:35:19 **零日志悬挂**，
心跳停在 04:35；05:00 因"进程恰好死了"才被 ALERT-DEAD 拉起 —— 这一天是靠运气救回来的。
若它挂着不死，就是 10-27 那种"白等 6 小时"（旧 HANG 分支只告警）。

本文件锁 7 条边界：
  1. 悬挂 + 当日 0 成功 + 有额度 ⇒ 杀进程再重启（顺序不能反：IgnoreNew 会忽略仍在跑的任务）
  2. 悬挂但**已有成功记录** ⇒ 不自动重启（可能正在写摘要/水位，强杀会留半截状态）
  3. 无进程 ⇒ 交给死亡分支，不在这里重复动作
  4. 每日额度用尽 ⇒ 只告警
  5. 杀不干净 ⇒ 放弃（双实例会互相覆盖同一 CSV）
  6. 杀失败 ⇒ 放弃
  7. dry-run ⇒ 不动作
"""
from __future__ import annotations

from datetime import datetime, timedelta

import watchdog


def _hb(**kw) -> dict:
    base = {
        "date": f"{datetime.now():%Y-%m-%d}",
        "status": "running",
        "started_at": (datetime.now() - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
        "collect_errors": 5,
        "last_success_at": None,
    }
    base.update(kw)
    return base


def _procs():
    return [(4321, "python.exe live_run.py")]


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "WATCHDOG_LOG", tmp_path / "logs" / "watchdog.log")
    monkeypatch.setattr(watchdog, "_sleep", lambda _s: None)


def _wire(monkeypatch, leftover=None):
    """装配：kill/restart 全部打桩，procs 第二次查询返回 leftover（默认空）。"""
    killed, calls = [], []
    monkeypatch.setattr(watchdog, "_kill_live_run",
                        lambda procs: (killed.append(list(procs)), True)[1])
    monkeypatch.setattr(watchdog, "_live_run_processes", lambda: list(leftover or []))
    monkeypatch.setattr(watchdog, "_restart", lambda r: calls.append(r))
    return killed, calls


def test_hang_restarts_when_never_succeeded(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    killed, calls = _wire(monkeypatch)
    assert watchdog._maybe_restart_hang(_hb(), None, _procs()) is True
    assert killed and killed[0][0][0] == 4321, "必须先杀进程"
    assert len(calls) == 1 and "悬挂" in calls[0], "杀完再 /Run"
    assert watchdog._restart_budget(_hb()["date"]) == 1


def test_hang_skipped_when_has_success(tmp_path, monkeypatch):
    """已有成功记录的悬挂不自动杀 —— 可能正在写摘要，强杀留半截状态。"""
    _isolate(tmp_path, monkeypatch)
    killed, calls = _wire(monkeypatch)
    assert watchdog._maybe_restart_hang(
        _hb(last_success_at="2026-10-28T07:31:28"), 600.0, _procs()) is False
    assert killed == [] and calls == []


def test_hang_skipped_without_process(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    killed, calls = _wire(monkeypatch)
    assert watchdog._maybe_restart_hang(_hb(), None, []) is False
    assert killed == [] and calls == []


def test_hang_respects_daily_budget(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    killed, calls = _wire(monkeypatch)
    day = _hb()["date"]
    for _ in range(watchdog.ZOMBIE_MAX_RESTARTS):
        watchdog._bump_restart_budget(day, "seed")
    assert watchdog._maybe_restart_hang(_hb(), None, _procs()) is False
    assert killed == [] and calls == []


def test_hang_aborts_when_process_survives(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    killed, calls = _wire(monkeypatch, leftover=_procs())
    assert watchdog._maybe_restart_hang(_hb(), None, _procs()) is False
    assert killed, "必须尝试过杀进程"
    assert calls == [], "残留进程时不得 /Run（会双实例写同一 CSV）"


def test_hang_aborts_when_kill_fails(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(watchdog, "_kill_live_run", lambda procs: False)
    monkeypatch.setattr(watchdog, "_live_run_processes", lambda: [])
    monkeypatch.setattr(watchdog, "_restart", lambda r: calls.append(r))
    assert watchdog._maybe_restart_hang(_hb(), None, _procs()) is False
    assert calls == []


def test_hang_dry_run_does_nothing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    killed, calls = _wire(monkeypatch)
    assert watchdog._maybe_restart_hang(_hb(), None, _procs(), dry=True) is False
    assert killed == [] and calls == []
