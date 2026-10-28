# -*- coding: utf-8 -*-
"""H63 + C 项回归：会话锁有界等待 / 开盘预热窗免费重建。

H63 背景（2026-10-28）：04:35 起进程**零日志悬挂** —— 主线程卡在等 `_AD_LOCK`，
`_run_with_deadline` 的 90s 硬超时只包住取数函数体、不包等锁，所以超时根本没生效。
合法持有者只有登录线程（上界 `_LOGIN_TIMEOUT=45s`），故等待上限 60s。

C 项背景：09:35 独立探针登录成功但当日 1m `rows=0`，10:00 新进程立刻采到 09:30 起全部 bar
⇒ AD 当日分钟线有约 30 分钟发布延迟。预热窗内"还没发布"不该按故障烧重建额度。

本文件锁 8 条：
  1. 锁被占用超时 ⇒ 抛 `AdLockTimeout`（kind=lock_timeout），且**不无限等**
  2. 一次超时后 60s 内快速失败（不再每轮白等）
  3. `_rebuild_session` 的锁超时**计入** rebuild_count（否则 H60 额度永远到不了上限）
  4. 预热窗 + 当日未成功 + kline_empty ⇒ 清缓存但**不扣额度**
  5. 窗口外 ⇒ 照常扣额度
  6. 当日已成功过 ⇒ 即使窗口内也照常扣额度
  7. 非 kline_empty（如 calendar_error）⇒ 不受豁免
  8. `_in_open_warmup` 边界（09:29 false / 09:30 true / 10:09 true / 10:10 false）
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core import data_fetch
from core.ad_errors import AdLockTimeout

BJ = ZoneInfo("Asia/Shanghai")


def _hold_lock(seconds: float = 0.6):
    """在另一个线程持有会话锁（RLock 跨线程互斥），返回 (thread, started_event)。"""
    ev = threading.Event()

    def _run():
        with data_fetch._AD_LOCK:
            ev.set()
            time.sleep(seconds)

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    assert ev.wait(2.0), "持锁线程未启动"
    return th


# ---------------------------------------------------------------- 1/2 有界等待
def test_lock_wait_times_out(monkeypatch):
    monkeypatch.setattr(data_fetch, "LOCK_WAIT_TIMEOUT", 0.2)
    data_fetch._SESSION["lock_blocked_at"] = 0.0
    th = _hold_lock(0.8)
    t0 = time.monotonic()
    with pytest.raises(AdLockTimeout) as ei:
        with data_fetch._locked("test"):
            pass
    spent = time.monotonic() - t0
    assert spent < 0.7, f"必须按上限返回而不是无限等（实际 {spent:.2f}s）"
    assert ei.value.kind == "lock_timeout" and ei.value.stage == "lock"
    th.join(2)


def test_second_call_fast_fails(monkeypatch):
    """一次超时后，60s 内后续调用立即失败（避免把 30s 节拍拖成 90s）。"""
    monkeypatch.setattr(data_fetch, "LOCK_WAIT_TIMEOUT", 0.1)
    data_fetch._SESSION["lock_blocked_at"] = 0.0
    th = _hold_lock(0.6)
    with pytest.raises(AdLockTimeout):
        with data_fetch._locked("first"):
            pass
    t0 = time.monotonic()
    with pytest.raises(AdLockTimeout):
        with data_fetch._locked("second"):
            pass
    assert time.monotonic() - t0 < 0.05, "第二次必须快速失败"
    th.join(2)


# ---------------------------------------------------------------- 3 计入额度
def test_rebuild_counts_lock_timeout(monkeypatch):
    monkeypatch.setattr(data_fetch, "LOCK_WAIT_TIMEOUT", 0.1)
    data_fetch._SESSION["lock_blocked_at"] = 0.0
    before = int(data_fetch._SESSION["rebuild_count"])
    th = _hold_lock(0.5)
    assert data_fetch._rebuild_session("kline_empty") is False
    th.join(2)
    assert int(data_fetch._SESSION["rebuild_count"]) == before + 1, \
        "锁超时必须计为一次已消耗的重建（否则 H60 的 exit(70) 永远不触发）"


# ---------------------------------------------------------------- 4/5/6/7 豁免
def _prepare(monkeypatch):
    monkeypatch.setattr(data_fetch, "REBUILD_MIN_INTERVAL", 0.0)
    data_fetch._SESSION["lock_blocked_at"] = 0.0
    return int(data_fetch._SESSION["rebuild_count"])


def test_warmup_empty_does_not_burn_quota(monkeypatch):
    monkeypatch.setattr(data_fetch, "_in_open_warmup", lambda *a, **k: True)
    data_fetch._SESSION["had_success"] = False
    before = _prepare(monkeypatch)
    assert data_fetch._rebuild_session("kline_empty") is True
    assert int(data_fetch._SESSION["rebuild_count"]) == before, "预热窗内不得扣额度"


def test_outside_warmup_burns_quota(monkeypatch):
    monkeypatch.setattr(data_fetch, "_in_open_warmup", lambda *a, **k: False)
    data_fetch._SESSION["had_success"] = False
    before = _prepare(monkeypatch)
    assert data_fetch._rebuild_session("kline_empty") is True
    assert int(data_fetch._SESSION["rebuild_count"]) == before + 1


def test_warmup_burns_quota_after_first_success(monkeypatch):
    monkeypatch.setattr(data_fetch, "_in_open_warmup", lambda *a, **k: True)
    data_fetch._SESSION["had_success"] = True          # 当日已成功过 ⇒ 不再是"还没发布"
    before = _prepare(monkeypatch)
    assert data_fetch._rebuild_session("kline_empty") is True
    assert int(data_fetch._SESSION["rebuild_count"]) == before + 1


def test_warmup_does_not_exempt_other_reasons(monkeypatch):
    monkeypatch.setattr(data_fetch, "_in_open_warmup", lambda *a, **k: True)
    data_fetch._SESSION["had_success"] = False
    before = _prepare(monkeypatch)
    assert data_fetch._rebuild_session("calendar_error") is True
    assert int(data_fetch._SESSION["rebuild_count"]) == before + 1


# ---------------------------------------------------------------- 8 窗口边界
@pytest.mark.parametrize("hhmm,expect", [
    ((9, 29), False),
    ((9, 30), True),
    ((10, 9), True),
    ((10, 10), False),
    ((14, 0), False),
])
def test_open_warmup_boundaries(monkeypatch, hhmm, expect):
    monkeypatch.setattr(data_fetch, "OPEN_WARMUP_END", (10, 10))
    now = datetime(2026, 10, 28, hhmm[0], hhmm[1], tzinfo=BJ)
    assert data_fetch._in_open_warmup(now) is expect
