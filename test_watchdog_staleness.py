# -*- coding: utf-8 -*-
"""H36 回归：循环活着但采集停摆时必须告警，且**绝不**触发重启。

背景（2026-10-19 D24）：H44 让 AD 查询通道间歇返空，进程每轮循环照跑、
`updated_at` 每轮必刷，而 `last_success_at` 冻结 33 分钟。看门狗只按
`updated_at` 判存活 ⇒ 全程判"健康"，故障无人发现。

本组用例锁两件事：
1. 陈旧必须**发出** ALERT-STALE（否则等于没修）；
2. 陈旧时**不得调用 _restart**（当日 5 次人工重启全败，盲目重启只会
   制造更多 ~2GB 僵尸进程并可能点燃 H37 的原生层 abort）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

import pytest

import watchdog


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    hb_path = tmp_path / "heartbeat.json"
    log_path = tmp_path / "logs" / "watchdog.log"
    monkeypatch.setattr(watchdog, "HEARTBEAT", hb_path)
    monkeypatch.setattr(watchdog, "WATCHDOG_LOG", log_path)
    monkeypatch.setattr(sys, "argv", ["watchdog.py", "--ignore-window"])
    return hb_path, log_path


def _heartbeat(**over):
    now = datetime.now()
    base = {
        "date": now.strftime("%Y-%m-%d"),
        "started_at": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
        "status": "running",
        "last_success_at": now.isoformat(timespec="seconds"),
        "collect_errors": 0,
        "match_errors": 0,
        "updated_at": now.isoformat(timespec="seconds"),
    }
    base.update(over)
    return base


def _write(hb_path, **over):
    hb_path.write_text(json.dumps(_heartbeat(**over), ensure_ascii=False),
                       encoding="utf-8")


def _log_text(log_path):
    return log_path.read_text(encoding="utf-8") if log_path.exists() else ""


def test_fresh_success_emits_no_stale_alert(sandbox):
    hb_path, log_path = sandbox
    _write(hb_path)

    assert watchdog.main() == 0

    text = _log_text(log_path)
    assert "[检查]" in text
    assert "ALERT-STALE" not in text


def test_stale_success_emits_alert(sandbox):
    hb_path, log_path = sandbox
    old = (datetime.now() - timedelta(seconds=1000)).isoformat(timespec="seconds")
    _write(hb_path, last_success_at=old)

    assert watchdog.main() == 0

    text = _log_text(log_path)
    assert "ALERT-STALE" in text
    assert "采集已停摆" in text
    assert "仅告警，不重启" in text


def test_threshold_boundary_below_does_not_fire(sandbox):
    """阈值 900s；850s 尚在正常波动内（正常日最大 695s），不得告警。"""
    hb_path, log_path = sandbox
    old = (datetime.now() - timedelta(seconds=850)).isoformat(timespec="seconds")
    _write(hb_path, last_success_at=old)

    assert watchdog.main() == 0
    assert "ALERT-STALE" not in _log_text(log_path)


def test_never_succeeded_while_running_emits_alert(sandbox):
    """启动后从未采到数据（登录失败/网络断）也属停摆，须吭声。"""
    hb_path, log_path = sandbox
    _write(hb_path, last_success_at=None)

    assert watchdog.main() == 0

    text = _log_text(log_path)
    assert "ALERT-STALE" in text
    assert "从未采集成功" in text


def test_never_succeeded_but_not_running_is_silent(sandbox):
    """status 非 running（已正常退出）时 last_success 为空属正常，不必告警。"""
    hb_path, log_path = sandbox
    _write(hb_path, last_success_at=None, status="stopped")

    assert watchdog.main() == 0
    assert "ALERT-STALE" not in _log_text(log_path)


def test_stale_alert_never_calls_restart(sandbox, monkeypatch):
    """**核心红线**：告警旁路不得触发任何重启动作。"""
    hb_path, _ = sandbox
    old = (datetime.now() - timedelta(seconds=3000)).isoformat(timespec="seconds")
    _write(hb_path, last_success_at=old)
    monkeypatch.setattr(watchdog, "_restart",
                        lambda *a, **k: pytest.fail("采集陈旧不得触发重启（H36）"))

    assert watchdog.main() == 0


def test_dead_process_still_restarts(sandbox, monkeypatch):
    """既有能力不能被削弱：心跳整体静默且无进程时，仍须重启。"""
    hb_path, _ = sandbox
    stale = (datetime.now() - timedelta(seconds=3000)).isoformat(timespec="seconds")
    _write(hb_path, updated_at=stale, last_success_at=stale)
    monkeypatch.setattr(watchdog, "_live_run_processes", lambda: [])
    monkeypatch.setattr(watchdog, "_today_watermark_reached_close", lambda: False)
    called = {"n": 0}

    def fake_restart(reason):
        called["n"] += 1

    monkeypatch.setattr(watchdog, "_restart", fake_restart)

    assert watchdog.main() == 0
    assert called["n"] == 1, "心跳静默 + 无进程 + 未收盘 → 必须重启"
