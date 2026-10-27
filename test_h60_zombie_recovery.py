# -*- coding: utf-8 -*-
"""H60 回归：① 重建额度耗尽 ⇒ 主动退出（每日上限内）；② 僵尸采集 ⇒ 受控重启。

背景（2026-10-27 实测）：04:32 首枪 `kline_empty`，全天 422 次失败、0 成功；
重建 20 次触顶后 `rebuild_rejected=252`，进程空转到 10:10 才死，白丢 6 小时；
15:30 看门狗重启后一次拉回 240 根 ⇒ "换进程"有效、"同进程重建"无解。

本文件锁 8 件事，每条配 guard-off（摘掉实现必变红）：
  1. 触顶只回调一次（252 次 rejected 不得刷出 252 次回调）
  2. 回调默认 None ⇒ 旧行为不变（回测/补数永不退出）
  3. 限额内退出、超限转告警
  4. 僵尸判定：启动时长 + 失败轮次双门槛
  5. 已有成功记录的陈旧不归此处（H36 告警旁路，10-19 五次重启全败）
  6. 每日重启额度硬上限
  7. 有残留进程时放弃重启（双实例会互相覆盖同一 CSV）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import watchdog
from core import data_fetch, live_exhaust


# ------------------------------------------------------------ 假构件
class _FakeLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def error(self, fmt: str, *a) -> None:
        self.lines.append(fmt % a if a else fmt)

    def exception(self, fmt: str, *a) -> None:
        self.lines.append(fmt % a if a else fmt)


def _hb(**kw) -> dict:
    """以「现在」为基准构造心跳，避免用例随日期漂移。"""
    base = {
        "date": f"{datetime.now():%Y-%m-%d}",
        "status": "running",
        "started_at": (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S"),
        "collect_errors": 422,
    }
    base.update(kw)
    return base


# ------------------------------------------------ 1. data_fetch 触顶回调
def test_hook_fires_once_when_cap_reached(monkeypatch):
    monkeypatch.setattr(data_fetch, "REBUILD_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(data_fetch, "REBUILD_DAILY_CAP", 2)
    seen: list[dict] = []
    data_fetch.set_exhausted_hook(seen.append)
    assert data_fetch._rebuild_session("a") is True
    assert data_fetch._rebuild_session("b") is True
    assert data_fetch._rebuild_session("c") is False      # 触顶
    assert data_fetch._rebuild_session("d") is False      # 继续被拒
    assert len(seen) == 1, "同一日只能回调一次（防 252 次 rejected 刷爆回调）"
    assert seen[0]["rebuild_count"] == 2
    assert seen[0]["rebuild_rejected"] >= 1
    assert seen[0]["reason"] == "c"


def test_no_hook_keeps_legacy_behaviour(monkeypatch):
    """默认 None ⇒ 只计数不回调（回测/批量补数不可能因额度耗尽退出）。"""
    monkeypatch.setattr(data_fetch, "REBUILD_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(data_fetch, "REBUILD_DAILY_CAP", 1)
    assert data_fetch._rebuild_session("a") is True
    assert data_fetch._rebuild_session("b") is False
    stats = data_fetch.session_stats()
    assert stats["rebuild_count"] == 1 and stats["rebuild_rejected"] == 1


# ------------------------------------------------ 2. 退出策略（core.live_exhaust）
def test_exhaust_hook_exits_and_leaves_marker(tmp_path):
    log = _FakeLogger()
    codes: list[int] = []
    hook = live_exhaust.make_hook(tmp_path, log, flush=lambda: None,
                                  exit_fn=codes.append, max_exits=2)
    hook({"day": "2026-10-27", "rebuild_count": 20, "rebuild_rejected": 252})
    assert codes == [live_exhaust.EXHAUST_EXIT_CODE]
    marker = live_exhaust.marker_path(tmp_path, "2026-10-27")
    state = json.loads(marker.read_text(encoding="utf-8"))
    assert state["count"] == 1
    assert state["detail"]["rebuild_count"] == 20


def test_exhaust_hook_stops_after_daily_cap(tmp_path):
    """第 2 次触顶不得再退出（防节假日"启动→触顶→退出"风暴）。"""
    log = _FakeLogger()
    codes: list[int] = []
    hook = live_exhaust.make_hook(tmp_path, log, flush=lambda: None,
                                  exit_fn=codes.append, max_exits=1)
    hook({"day": "2026-10-27"})
    hook({"day": "2026-10-27"})
    assert codes == [live_exhaust.EXHAUST_EXIT_CODE]
    state = json.loads(live_exhaust.marker_path(tmp_path, "2026-10-27")
                       .read_text(encoding="utf-8"))
    assert state["count"] == 2
    assert any("不再退出" in s for s in log.lines)


def test_should_exit_boundary():
    assert live_exhaust.should_exit(1, 2) is True
    assert live_exhaust.should_exit(2, 2) is True
    assert live_exhaust.should_exit(3, 2) is False


# ------------------------------------------------ 3. 僵尸采集判定
def test_zombie_true_when_never_succeeded_long_enough():
    ok, reason = watchdog._zombie_restart_decision(_hb(), None)
    assert ok is True and "0 成功" in reason


def test_zombie_false_during_warmup():
    """启动 10 分钟还不到重启门槛（正常首轮可能很慢）。"""
    ok, reason = watchdog._zombie_restart_decision(
        _hb(started_at=(datetime.now() - timedelta(minutes=10))
            .strftime("%Y-%m-%d %H:%M:%S")), None)
    assert ok is False and "启动仅" in reason


def test_zombie_false_when_errors_too_few():
    ok, _reason = watchdog._zombie_restart_decision(_hb(collect_errors=5), None)
    assert ok is False


def test_zombie_false_when_status_not_running():
    ok, _reason = watchdog._zombie_restart_decision(_hb(status="starting"), None)
    assert ok is False


def test_zombie_false_when_has_success_history():
    """已有成功记录后的陈旧 ⇒ 归 H36 告警旁路，不做进程重启（10-19 五次重启全败）。"""
    ok, reason = watchdog._zombie_restart_decision(_hb(), 1800)
    assert ok is False and "H36" in reason


# ------------------------------------------------ 4. 受控重启的额度与安全门
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "WATCHDOG_LOG", tmp_path / "logs" / "watchdog.log")
    monkeypatch.setattr(watchdog, "_sleep", lambda _s: None)


def test_maybe_restart_respects_daily_budget(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(watchdog, "_live_run_processes", lambda: [])
    calls: list[str] = []
    monkeypatch.setattr(watchdog, "_restart", lambda r: calls.append(r))
    hb = _hb()
    for _ in range(watchdog.ZOMBIE_MAX_RESTARTS):
        watchdog._bump_restart_budget(hb["date"], "seed")
    assert watchdog._maybe_restart_zombie(hb, None) is False
    assert calls == [], "额度用尽后不得再重启"


def test_maybe_restart_kills_then_relaunches(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    seq = {"n": 0}

    def fake_procs():
        seq["n"] += 1
        return [(1234, "python.exe live_run.py")] if seq["n"] == 1 else []

    monkeypatch.setattr(watchdog, "_live_run_processes", fake_procs)
    killed: list[list] = []

    def fake_kill(procs):
        killed.append(list(procs))
        return True

    monkeypatch.setattr(watchdog, "_kill_live_run", fake_kill)
    calls: list[str] = []
    monkeypatch.setattr(watchdog, "_restart", lambda r: calls.append(r))

    assert watchdog._maybe_restart_zombie(_hb(), None) is True
    assert killed and killed[0][0][0] == 1234
    assert len(calls) == 1 and "僵尸采集" in calls[0]
    assert watchdog._restart_budget(_hb()["date"]) == 1


def test_maybe_restart_aborts_when_process_survives(tmp_path, monkeypatch):
    """杀不死就放弃（双实例会互相覆盖同一 CSV，比不重启更危险）。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(watchdog, "_live_run_processes",
                        lambda: [(1234, "python.exe live_run.py")])
    monkeypatch.setattr(watchdog, "_kill_live_run", lambda procs: True)
    calls: list[str] = []
    monkeypatch.setattr(watchdog, "_restart", lambda r: calls.append(r))
    assert watchdog._maybe_restart_zombie(_hb(), None) is False
    assert calls == []


def test_maybe_restart_aborts_when_kill_fails(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(watchdog, "_live_run_processes",
                        lambda: [(1234, "python.exe live_run.py")])
    monkeypatch.setattr(watchdog, "_kill_live_run", lambda procs: False)
    calls: list[str] = []
    monkeypatch.setattr(watchdog, "_restart", lambda r: calls.append(r))
    assert watchdog._maybe_restart_zombie(_hb(), None) is False
    assert calls == []
