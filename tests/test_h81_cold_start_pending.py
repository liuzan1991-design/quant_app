# -*- coding: utf-8 -*-
"""H81 回归（2026-11-12 立号）：验证窗口与冷启动对齐 + pending 跨轮次 + 非交易时段放宽水位。

立号事实（11-12 实测，非估算）
------------------------------
- 04:30:12 启动 → 04:34:46 首次采集成功 ⇒ **冷启动 274s**；
- 07:05:25 重启 → 07:09:58 首次采集成功 ⇒ **冷启动 273s**；
- 旧 `STALL_VERIFY_TIMEOUT=180s` ⇒ 07:08:17 判 no_recovery，比首次成功**早 101 秒**，
  属结构性假失败：重启其实成功，却锁死当日额度并留下永久告警。

本组用例锁死三件事
------------------
1. 冷启动期内「还没上线 / 还没首次成功」只能是 **pending**，不得判 failed；
2. pending 落账本后跨轮次续验：不重复 kill、不重复吃额度、成功要清告警；
3. 非交易时段（午休/盘前/盘后）**水位不变属正常**，但倒退仍判失败。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest

import watchdog as w

BJT3 = timezone(timedelta(hours=3))      # 本机时区（GMT+3），用它构造确定性时刻
DAY = "2026-11-12"
ATTEMPT = datetime(2026, 11, 12, 7, 5, 4)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "LIVE_DIR", tmp_path)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "heartbeat.json")
    monkeypatch.setattr(w, "WATCHDOG_LOG", tmp_path / "logs" / "watchdog.log")
    monkeypatch.setattr(w, "STALL_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(w, "PROC_QUERY_TRUSTED", True)
    return tmp_path


def _snap(**over):
    base = {"pids": [33212], "started_at": "2026-11-12T07:05:25",
            "last_success_at": None, "last_bar_at": "2026-11-12T11:29:00"}
    base.update(over)
    return base


PREV = {"pids": [32524], "started_at": "2026-11-12T04:30:11",
        "last_success_at": "2026-11-12T06:49:11", "last_bar_at": "2026-11-12T11:29:00"}


# ── 一、冷启动期内不得判死 ─────────────────────────────────────────────────

def test_pending_while_new_instance_not_yet_up():
    """重启后头几秒：旧实例已死（进程列表空）、新实例还没写心跳 ⇒ 是"还没起"，不是"恢复失败"。"""
    cur = _snap(pids=[], started_at="2026-11-12T04:30:11", last_success_at="2026-11-12T06:49:11")
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=61), session="lunch")
    assert st == "pending"
    assert "尚未上线" in why and "61s/420s" in why


def test_pending_while_started_but_no_success_yet():
    """实例已起、还没采到数 ⇒ pending（11-12 07:08 的真实现场）。"""
    cur = _snap(last_success_at=None)
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=193), session="lunch")
    assert st == "pending"
    assert "尚未采集成功" in why


def test_failed_after_cold_start_cap():
    """超过冷启动上限（420s）仍无首次成功 ⇒ 才是真失败。"""
    cur = _snap(last_success_at=None)
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=421), session="lunch")
    assert st == "failed"
    assert "421s/420s" in why


def test_pending_boundary_before_cap():
    """420s 整仍在窗口内 ⇒ pending（边界不误杀）。"""
    cur = _snap(last_success_at=None)
    st, _ = w._recovery_verdict(PREV, cur, ATTEMPT,
                                now=ATTEMPT + timedelta(seconds=419), session="lunch")
    assert st == "pending"


def test_future_attempt_ts_is_failed_not_infinite_pending():
    """账本时刻来自未来（时钟回拨）⇒ 用时为负，不能靠"再等等"把额度无限挂住。"""
    cur = _snap(last_success_at=None)
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT + timedelta(minutes=10),
                                  now=ATTEMPT, session="lunch")
    assert st == "failed" and "尝试时刻在未来" in why


def test_old_pid_alive_is_failed_even_inside_window():
    """旧 PID 仍存活是硬边界，不许用"再等等"换时间（双实例写同一 CSV）。"""
    cur = _snap(pids=[32524], started_at="2026-11-12T07:05:25",
                last_success_at="2026-11-12T07:09:58")
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=30), session="lunch")
    assert st == "failed" and "旧 PID" in why


# ── 二、非交易时段放宽水位判据 ─────────────────────────────────────────────

def test_lunch_ignores_flat_watermark():
    """午休不产 bar：last_success 前进 + 水位不变 ⇒ 视为恢复。"""
    cur = _snap(last_success_at="2026-11-12T12:09:58")
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=200), session="lunch")
    assert st == "recovered"
    assert "非交易时段（lunch）" in why


def test_post_close_ignores_flat_watermark():
    cur = _snap(last_success_at="2026-11-12T15:02:10")
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=200), session="post_close")
    assert st == "recovered" and "非交易时段（post_close）" in why


def test_in_session_still_requires_watermark_advance():
    """盘中口径不放宽：午休那套不能顺手用到盘中，否则"重读旧数据"会假绿。"""
    cur = _snap(last_success_at="2026-11-12T13:05:10",
                last_bar_at="2026-11-12T11:29:00")
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=200), session="in_session")
    assert st == "failed" and "水位未推进" in why


def test_non_session_watermark_backwards_is_failed():
    """非交易时段只放宽"不变"，倒退仍是异常（CSV 被截断/重建）。"""
    cur = _snap(last_success_at="2026-11-12T12:09:58",
                last_bar_at="2026-11-12T10:59:00")
    st, why = w._recovery_verdict(PREV, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=200), session="lunch")
    assert st == "failed" and "水位倒退" in why


# ── 三、pending 跨轮次：不吃额度、不重复杀、成功清告警 ──────────────────────

def _wire_restart(monkeypatch, verify=("pending", "新实例尚未采集成功")):
    calls = {"kill": 0, "restart": 0, "cleared": []}
    state = {"procs": [(32524, "live_run.py")]}

    monkeypatch.setattr(w, "_error_delta", lambda day, cur: (6, 200))
    monkeypatch.setattr(w, "_save_collect_errors", lambda day, cur: None)
    monkeypatch.setattr(w, "_today_watermark_reached_close", lambda: False)
    monkeypatch.setattr(w, "_live_run_processes", lambda: list(state["procs"]))
    monkeypatch.setattr(w, "_live_run_rss_mb", lambda procs: 107.0)
    monkeypatch.setattr(w, "_recovery_snapshot", lambda day=None: PREV)

    def _kill(procs):
        calls["kill"] += 1
        state["procs"] = []
        return True

    def _restart(reason, wait=True):
        calls["restart"] += 1

    monkeypatch.setattr(w, "_kill_live_run", _kill)
    monkeypatch.setattr(w, "_restart", _restart)
    monkeypatch.setattr(w, "_verify_recovery", lambda prev, ts: verify)
    monkeypatch.setattr(w, "raise_alert", lambda *a, **k: None)
    monkeypatch.setattr(w, "clear_alert", lambda kind, *a, **k: calls["cleared"].append(kind))
    return calls


def _hb(**over):
    base = {"date": DAY, "status": "running",
            "started_at": "2026-11-12T04:30:11",
            "last_success_at": "2026-11-12T06:49:11",
            "collect_errors": 230,
            "updated_at": "2026-11-12T07:05:00"}
    base.update(over)
    return base


def test_pending_is_recorded_without_locking_the_day(sandbox, monkeypatch):
    calls = _wire_restart(monkeypatch)
    assert w._maybe_restart_stall(_hb(), 16 * 60) is False
    led = w._load_stall_ledger(DAY)
    assert calls == {"kill": 1, "restart": 1, "cleared": []}
    assert led["attempts"] == 1
    assert led["stop_reason"] is None          # 关键：pending 不锁当日额度
    assert led["last_outcome"] == "pending"
    assert led["pending"]["attempt_at"] and led["pending"]["prev"]["pids"] == [32524]
    # 额度仍可用
    assert w._stall_gate(led)[0] is True


def test_resume_pending_success_without_second_kill(sandbox, monkeypatch):
    w._save_stall_ledger(DAY, attempts=1, last_outcome="pending",
                         pending={"attempt_at": ATTEMPT.isoformat(timespec="seconds"),
                                  "prev": PREV})
    calls = _wire_restart(monkeypatch, verify=("recovered", "实例 [33212]、last_success 07:09:58"))
    assert w._maybe_restart_stall(_hb(), None) is True
    led = w._load_stall_ledger(DAY)
    assert calls["kill"] == 0 and calls["restart"] == 0     # 续验不得再动手
    assert led["pending"] is None
    assert led["successes"] == 1
    assert led["attempts"] == 1                             # 续验不吃额度
    assert led["stop_reason"] is None
    # H81：假失败留下的 stall_no_recovery 必须一并撤掉
    assert "stall_no_recovery" in calls["cleared"]


def test_resume_pending_still_pending_keeps_waiting(sandbox, monkeypatch):
    w._save_stall_ledger(DAY, attempts=1, last_outcome="pending",
                         pending={"attempt_at": ATTEMPT.isoformat(timespec="seconds"),
                                  "prev": PREV})
    calls = _wire_restart(monkeypatch)
    assert w._maybe_restart_stall(_hb(), None) is False
    led = w._load_stall_ledger(DAY)
    assert led["pending"] is not None and led["pending"].get("checked_at")
    assert led["stop_reason"] is None
    assert calls["kill"] == 0 and calls["restart"] == 0


def test_resume_pending_failure_locks_the_day(sandbox, monkeypatch):
    w._save_stall_ledger(DAY, attempts=1, last_outcome="pending",
                         pending={"attempt_at": ATTEMPT.isoformat(timespec="seconds"),
                                  "prev": PREV})
    _wire_restart(monkeypatch, verify=("failed", "421s/420s 仍无首次成功"))
    assert w._maybe_restart_stall(_hb(), None) is False
    led = w._load_stall_ledger(DAY)
    assert led["pending"] is None
    assert led["failures"] == 1
    assert "421s/420s" in led["stop_reason"]
    assert w._stall_gate(led)[0] is False


# ── 四、main 分支顺序：pending 优先于「进程缺失」判据 ──────────────────────

def test_main_does_not_restart_again_while_pending(sandbox, monkeypatch):
    """心跳已停 600s + 无进程时，旧逻辑会立刻 `/Run`；有 pending 时必须只续验。"""
    w.HEARTBEAT.write_text(json.dumps({
        "date": DAY, "status": "running",
        "started_at": "2026-11-12T04:30:11",
        "last_success_at": "2026-11-12T06:49:11",
        "updated_at": "2026-11-12T06:55:00",
        "collect_errors": 12,
    }), encoding="utf-8")
    w._save_stall_ledger(DAY, attempts=1, last_outcome="pending",
                         pending={"attempt_at": ATTEMPT.isoformat(timespec="seconds"),
                                  "prev": PREV})
    calls = _wire_restart(monkeypatch)
    monkeypatch.setattr(w, "_live_run_processes", lambda: [])
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--ignore-window"])

    assert w.main() == 0
    assert calls["restart"] == 0
    assert w._load_stall_ledger(DAY)["pending"] is not None


# ── 五、时区：本机 GMT+3 ≠ 北京 ────────────────────────────────────────────

def test_bj_now_converts_local_to_beijing():
    local = datetime(2026, 11, 12, 7, 5, 4, tzinfo=BJT3)     # 本机 07:05 = 北京 12:05
    assert w._bj_now(local).strftime("%H:%M") == "12:05"


def test_session_now_recognizes_lunch():
    local = datetime(2026, 11, 12, 7, 5, 4, tzinfo=BJT3)     # 北京 12:05 午休
    assert w._session_now(local) == "lunch"


def test_session_now_recognizes_in_session():
    local = datetime(2026, 11, 12, 2, 5, 0, tzinfo=BJT3)     # 北京 07:05 盘前
    assert w._session_now(local) == "pre_open"
    local2 = datetime(2026, 11, 12, 4, 45, 0, tzinfo=BJT3)   # 北京 09:45 盘中
    assert w._session_now(local2) == "in_session"
