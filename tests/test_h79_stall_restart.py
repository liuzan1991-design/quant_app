# -*- coding: utf-8 -*-
"""H79 回归：成功停摆的受控重启必须"失败关闭 + 恢复可验证 + 失败即停"。

背景（2026-11-10）：AD SDK 会话卡死，进程每轮 90s 硬超时、心跳照刷 3.5 小时，
看门狗只告警不动作。当日人工重启 11.8s 恢复。

旧实现（39bd2c5）复核发现三个问题，本组用例逐条锁死：
1. `RSS 未知` 被当"没有内存泄漏风险"放行 ⇒ 现改为**失败关闭**；
2. 进程查询不可信（PowerShell 空 stdout）时 `procs=[]` → 跳过 kill 直接 `/Run`
   ⇒ 可能双实例写同一 CSV；现改为**失败关闭**；
3. 重启成功判据是"心跳 20s 内刷新"，而停摆进程本来就刷心跳 ⇒ 假绿；
   现改为**新实例身份 + last_success_at 前进 + CSV 水位推进**三重验证。
另外：尝试次数与恢复成功次数分开记账，一次恢复失败即停止当日自动重试。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import watchdog as w


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "LIVE_DIR", tmp_path)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "heartbeat.json")
    monkeypatch.setattr(w, "WATCHDOG_LOG", tmp_path / "logs" / "watchdog.log")
    monkeypatch.setattr(w, "STALL_DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(w, "PROC_QUERY_TRUSTED", True)
    return tmp_path


def _hb(**over):
    now = datetime.now()
    base = {"date": now.strftime("%Y-%m-%d"),
            "started_at": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
            "status": "running",
            "last_success_at": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
            "collect_errors": 230,
            "updated_at": now.isoformat(timespec="seconds")}
    base.update(over)
    return base


# ── 一、判据：失败关闭 ─────────────────────────────────────────────────────

def test_rss_unknown_blocks_restart():
    need, reason = w._success_stall_decision(_hb(), 3600, 6, None)
    assert need is False
    assert "RSS 未知" in reason


def test_untrusted_proc_query_blocks_restart():
    need, reason = w._success_stall_decision(
        _hb(), 3600, 6, 107.0, proc_query_trusted=False)
    assert need is False
    assert "不可信" in reason


def test_no_process_blocks_restart():
    need, reason = w._success_stall_decision(_hb(), 3600, 6, 107.0, proc_count=0)
    assert need is False
    assert "无 live_run 进程" in reason


def test_memory_leak_type_still_blocked():
    need, reason = w._success_stall_decision(_hb(), 3600, 6, 2000.0)
    assert need is False
    assert "内存泄漏" in reason


def test_healthy_known_state_allows_restart():
    need, reason = w._success_stall_decision(_hb(), 3600, 6, 107.0, proc_count=1)
    assert need is True
    assert "RSS 107MB" in reason


# ── 二、账本：尝试/成功分开、失败即停 ───────────────────────────────────────

def test_gate_blocks_after_failure():
    ok, why = w._stall_gate({"attempts": 1, "stop_reason": "no_recovery：水位未推进"})
    assert ok is False and "已停止自动重试" in why


def test_gate_blocks_at_attempt_cap():
    ok, why = w._stall_gate({"attempts": w.STALL_MAX_ATTEMPTS})
    assert ok is False and "上限" in why


def test_ledger_keeps_attempts_and_successes_separate(sandbox):
    day = "2026-11-10"
    w._save_stall_ledger(day, attempts=1, successes=1, last_outcome="recovered")
    led = w._load_stall_ledger(day)
    assert led["attempts"] == 1 and led["successes"] == 1
    assert led["stop_reason"] is None


# ── 三、三重恢复验证 ────────────────────────────────────────────────────────

def _snap(**over):
    base = {"pids": [200], "started_at": "2026-11-10T13:45:00",
            "last_success_at": "2026-11-10T13:45:30",
            "last_bar_at": "2026-11-10T13:44:00"}
    base.update(over)
    return base


ATTEMPT = datetime(2026, 11, 10, 13, 40, 0)


def test_verdict_requires_new_instance_identity():
    prev = _snap(pids=[100])
    later = ATTEMPT + timedelta(seconds=600)          # 已过冷启动上限
    st, why = w._recovery_verdict(prev, _snap(pids=[100]), ATTEMPT, now=later)
    assert st == "failed" and "旧 PID" in why
    st, why = w._recovery_verdict(prev, _snap(started_at="2026-11-10T13:30:00"), ATTEMPT, now=later)
    assert st == "failed" and "尚未上线" in why
    st, why = w._recovery_verdict(prev, _snap(pids=None), ATTEMPT, now=later)
    assert st == "failed" and "无法确认" in why


def test_verdict_requires_last_success_advance():
    prev = _snap(pids=[100], last_success_at="2026-11-10T13:45:30")
    later = ATTEMPT + timedelta(seconds=600)
    st, why = w._recovery_verdict(prev, _snap(last_success_at="2026-11-10T13:45:30"),
                                  ATTEMPT, now=later)
    assert st == "failed" and "尚未采集成功" in why


def test_verdict_requires_bar_watermark_advance():
    """重读旧数据不算恢复：last_success 前进但水位没动 ⇒ 失败（盘中口径）。"""
    prev = _snap(pids=[100], last_bar_at="2026-11-10T13:44:00")
    cur = _snap(last_bar_at="2026-11-10T13:44:00",
                last_success_at="2026-11-10T13:45:59")
    st, why = w._recovery_verdict(prev, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=120),
                                  session="in_session")
    assert st == "failed" and "水位未推进" in why


def test_verdict_passes_only_when_all_three_hold():
    prev = _snap(pids=[100], last_success_at="2026-11-10T13:40:10",
                 last_bar_at="2026-11-10T13:39:00", started_at="2026-11-10T08:40:00")
    cur = _snap(pids=[200], started_at="2026-11-10T13:45:00",
                last_success_at="2026-11-10T13:45:30", last_bar_at="2026-11-10T13:44:00")
    st, why = w._recovery_verdict(prev, cur, ATTEMPT,
                                  now=ATTEMPT + timedelta(seconds=120),
                                  session="in_session")
    assert st == "recovered" and "实例 [200]" in why


# ── 四、端到端动作链 ───────────────────────────────────────────────────────

def _wire(monkeypatch, verify=("recovered", "三重验证通过"), kill_ok=True, procs_after=()):
    calls = {"kill": 0, "restart": 0}
    state = {"procs": [(100, "live_run.py")]}

    monkeypatch.setattr(w, "_error_delta", lambda day, cur: (6, 200))
    monkeypatch.setattr(w, "_save_collect_errors", lambda day, cur: None)
    monkeypatch.setattr(w, "_today_watermark_reached_close", lambda: False)
    monkeypatch.setattr(w, "_live_run_processes", lambda: list(state["procs"]))
    monkeypatch.setattr(w, "_live_run_rss_mb", lambda procs: 107.0)
    monkeypatch.setattr(w, "_recovery_snapshot", lambda day=None: _snap(pids=[100]))

    def _kill(procs):
        calls["kill"] += 1
        if kill_ok:
            state["procs"] = list(procs_after)
        return kill_ok

    def _restart(reason, wait=True):
        calls["restart"] += 1

    monkeypatch.setattr(w, "_kill_live_run", _kill)
    monkeypatch.setattr(w, "_restart", _restart)
    monkeypatch.setattr(w, "_verify_recovery", lambda prev, ts: verify)
    monkeypatch.setattr(w, "raise_alert", lambda *a, **k: None)
    monkeypatch.setattr(w, "clear_alert", lambda *a, **k: None)
    return calls


def test_restart_success_records_attempt_and_recovery(sandbox, monkeypatch):
    calls = _wire(monkeypatch)
    assert w._maybe_restart_stall(_hb(), 3600) is True
    led = w._load_stall_ledger(_hb()["date"])
    assert calls == {"kill": 1, "restart": 1}
    assert led["attempts"] == 1 and led["successes"] == 1
    assert led["last_outcome"] == "recovered"


def test_failed_recovery_stops_second_attempt(sandbox, monkeypatch):
    calls = _wire(monkeypatch, verify=("failed", "CSV 水位未推进（旧数据重读）"))
    assert w._maybe_restart_stall(_hb(), 3600) is False
    led = w._load_stall_ledger(_hb()["date"])
    assert led["attempts"] == 1 and led["successes"] == 0
    assert "水位未推进" in led["stop_reason"]
    # 第二轮：不得再杀再拉（旧实现会反复重启）
    assert w._maybe_restart_stall(_hb(), 3600) is False
    assert calls == {"kill": 1, "restart": 1}


def test_kill_failure_is_counted_and_stops(sandbox, monkeypatch):
    calls = _wire(monkeypatch, kill_ok=False)
    assert w._maybe_restart_stall(_hb(), 3600) is False
    led = w._load_stall_ledger(_hb()["date"])
    assert led["last_outcome"] == "kill_failed"
    assert calls["restart"] == 0           # 没杀干净不许拉起
    assert w._maybe_restart_stall(_hb(), 3600) is False
    assert calls["kill"] == 1              # 已停止自动重试


def test_untrusted_query_never_touches_process(sandbox, monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(w, "PROC_QUERY_TRUSTED", False)
    assert w._maybe_restart_stall(_hb(), 3600) is False
    assert calls == {"kill": 0, "restart": 0}


def test_restart_log_never_claims_success_from_heartbeat(sandbox, monkeypatch, tmp_path):
    """`_restart` 只报告"已触发/进程已起"，成功判据交给 H79 的三重验证。"""
    class _R:
        returncode = 0

    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: _R())
    w.HEARTBEAT.write_text(json.dumps(
        {"updated_at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    w._restart("测试", wait=True)
    text = (tmp_path / "logs" / "watchdog.log").read_text(encoding="utf-8")
    assert "未证明采数恢复" in text
    assert "✅ 重启生效" not in text
