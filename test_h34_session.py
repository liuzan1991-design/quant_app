# -*- coding: utf-8 -*-
"""H34 余项回归：日历缓存 + 被动重建 + 熔断 + 登录单飞（不登录、不触网、不写生产目录）。

背景（28 份 live_*.log 全量统计，09-09~10-22）：
- `get_calendar()` 相关失败 360 次（42%），但这条路径**直接 raise**，绕过了
  `data_fetch.py:173` 那条唯一会清缓存重登的分支（kline_empty，仅 71 次 / 8%）。
- `_AD_CACHE` 是裸 dict，硬超时放弃线程后新线程再次登录 ⇒ 并发互踢。

本文件锁住 10 件事，每条都配 guard-off（摘掉对应实现必须变红，见 §6 mutation 表）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime

import pandas as pd
import pytest

from core import data_fetch
from core.ad_errors import (
    AdCalendarError, AdCircuitOpen, AdLoginTimeout, AdSdkError,
)

CODE = "300308"
DAY = "2026-10-22"


# --------------------------------------------------------------- 假 SDK 构件
class _FakePeriod:
    class min1:
        value = 8


class _FakeConstant:
    Period = _FakePeriod


class _FakeAd:
    """同形替身：BaseData/get_calendar/MarketData/query_kline 都在同一对象上。"""

    constant = _FakeConstant

    def __init__(self, calendar_seq=None, kline_seq=None):
        self.calendar_seq = list(calendar_seq or [object()])
        self.kline_seq = list(kline_seq or [_ok_frame()])
        self.calls = {"calendar": 0, "kline": 0}

    def BaseData(self):
        return self

    def get_calendar(self):
        self.calls["calendar"] += 1
        item = self.calendar_seq[min(self.calls["calendar"] - 1,
                                    len(self.calendar_seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    def MarketData(self, calendar):
        return self

    def query_kline(self, codes, **kwargs):
        self.calls["kline"] += 1
        item = self.kline_seq[min(self.calls["kline"] - 1,
                                  len(self.kline_seq) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _ok_frame():
    return {"300308.SZ": pd.DataFrame({
        "time": ["2026-10-22 09:30:00", "2026-10-22 09:31:00"],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [100, 200], "amount": [10.0, 20.0],
    })}


@pytest.fixture
def stub_login(monkeypatch):
    """桩掉 `_login`，让日历/重建逻辑在纯离线条件下可测。"""
    state = {"ad": None, "calls": 0}

    def _fake_login():
        state["calls"] += 1
        return state["ad"]

    monkeypatch.setattr(data_fetch, "_login", _fake_login)
    monkeypatch.setattr(data_fetch, "_AD_CACHE", {})
    return state


# ------------------------------------------------------- 1 日历缓存（降暴露面）
def test_calendar_cached_within_day(stub_login):
    """同一天两次 fetch ⇒ `get_calendar()` 只被调用 1 次。

    guard-off：摘掉 `_get_calendar` 的缓存读写 ⇒ 变 2 次 ⇒ 红。
    """
    ad = _FakeAd()
    stub_login["ad"] = ad
    data_fetch.fetch_kline(CODE, DAY, DAY)
    data_fetch.fetch_kline(CODE, DAY, DAY)
    assert ad.calls["calendar"] == 1, f"实测调用 {ad.calls['calendar']} 次，缓存未生效"
    assert ad.calls["kline"] == 2


def test_calendar_cache_invalidated_on_day_change(stub_login):
    """缓存日期不是今天 ⇒ 必须重新取（跨日不得复用旧日历对象）。"""
    old = object()
    data_fetch._AD_CACHE["calendar"] = {"day": "1999-01-01", "obj": old}
    ad = _FakeAd()
    stub_login["ad"] = ad
    data_fetch.fetch_kline(CODE, DAY, DAY)
    assert ad.calls["calendar"] == 1, "跨日仍复用旧缓存"
    cached = data_fetch._AD_CACHE.get("calendar") or {}
    assert cached.get("day") == datetime.now().strftime("%Y-%m-%d")
    assert cached.get("obj") is not old


# --------------------------------------------------------- 2 被动重建（统一入口）
def test_calendar_error_triggers_rebuild(stub_login):
    """calendar 首次抛错 ⇒ 清缓存 + 本轮重试一次（第二次 `_login` 自然重登）。

    guard-off：摘掉 `_rebuild_session` 调用 ⇒ 不重试、直接抛原错 ⇒ 红。
    """
    ad = _FakeAd(calendar_seq=[TypeError("'NoneType' object is not subscriptable"),
                               object()])
    stub_login["ad"] = ad
    df = data_fetch.fetch_kline(CODE, DAY, DAY)
    assert len(df) == 2, "重建后未能取到数据"
    assert ad.calls["calendar"] == 2
    assert stub_login["calls"] == 2, "重建后没有重新登录"
    stats = data_fetch.session_stats()
    assert stats["rebuild_count"] == 1


def test_rebuild_rate_limited(monkeypatch):
    """60s 内第二次重建必须被拒（否则一次故障会被连续重建，打出多个登录线程）。"""
    monkeypatch.setattr(data_fetch, "REBUILD_MIN_INTERVAL", 60.0)
    assert data_fetch._rebuild_session("t1") is True
    assert data_fetch._rebuild_session("t2") is False
    stats = data_fetch.session_stats()
    assert stats["rebuild_count"] == 1 and stats["rebuild_rejected"] == 1


def test_rebuild_daily_cap(monkeypatch):
    """单日上限 20 次：第 21 次必须被拒（最坏日实测 197 块失败）。"""
    monkeypatch.setattr(data_fetch, "REBUILD_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(data_fetch, "REBUILD_DAILY_CAP", 20)
    ok = sum(1 for _ in range(20) if data_fetch._rebuild_session("x"))
    assert ok == 20
    assert data_fetch._rebuild_session("x") is False
    assert data_fetch.session_stats()["rebuild_count"] == 20


# --------------------------------------------------------------- 3 登录单飞
def test_login_single_flight(monkeypatch):
    """8 线程并发 `_login()` ⇒ 真实 `login` 恰好 1 次（并发登录 = AD 互踢）。"""
    counter = {"n": 0}

    class _FakeModule:
        constant = _FakeConstant

        @staticmethod
        def login(**kwargs):
            counter["n"] += 1
            time.sleep(0.2)

    # 从 `_import_ad` 注入：H19 守卫装的是 `_login`，直接写 sys.modules 也不够。
    # 单飞锁在 `_login_impl` 里，测试走实现体（生产唯一入口 `_login` 仍受守卫保护）。
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: _FakeModule())
    monkeypatch.setattr(data_fetch, "load_credentials",
                        lambda: {"username": "u", "password": "p",
                                 "host": "h", "port": 1})
    data_fetch._AD_CACHE.pop("ad", None)

    errors = []

    def _hit():
        try:
            data_fetch._login_impl()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_hit, daemon=True) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)
    assert not errors, f"并发登录出现异常：{errors}"
    assert counter["n"] == 1, f"真实 login 被调用 {counter['n']} 次（缺单飞锁）"


# ---------------------------------------------------------------- 4 熔断三态
def test_breaker_opens_after_5(stub_login, monkeypatch):
    """连续 5 次 login_timeout ⇒ 第 6 次不触 SDK、快速失败（elapsed≈0）。"""
    calls = {"n": 0}

    def _boom(*args, **kwargs):
        calls["n"] += 1
        raise AdLoginTimeout("登录超时", stage="login")

    monkeypatch.setattr(data_fetch, "_fetch_kline_inner", _boom)
    for _ in range(5):
        with pytest.raises(AdLoginTimeout):
            data_fetch.fetch_kline(CODE, DAY, DAY)
    assert data_fetch.session_stats()["breaker_state"] == "open"
    assert calls["n"] == 5

    t0 = time.monotonic()
    with pytest.raises(AdCircuitOpen) as ei:
        data_fetch.fetch_kline(CODE, DAY, DAY)
    elapsed = time.monotonic() - t0
    assert ei.value.kind == "circuit_open" and ei.value.stage == "circuit"
    assert calls["n"] == 5, "开闸期间仍调用了 SDK"
    assert elapsed < 0.1, f"快速失败耗时 {elapsed:.3f}s"


def test_breaker_half_open_probe(stub_login, monkeypatch):
    """冷却到期 ⇒ 放 1 个探针；探针成功 ⇒ closed（不能"永不恢复"）。"""
    monkeypatch.setattr(data_fetch, "BREAKER_COOLDOWN", 0.0)

    def _boom(*args, **kwargs):
        raise AdLoginTimeout("登录超时", stage="login")

    monkeypatch.setattr(data_fetch, "_fetch_kline_inner", _boom)
    for _ in range(5):
        with pytest.raises(AdLoginTimeout):
            data_fetch.fetch_kline(CODE, DAY, DAY)

    monkeypatch.setattr(data_fetch, "_fetch_kline_inner",
                        lambda *a, **k: _ok_frame()["300308.SZ"])
    df = data_fetch.fetch_kline(CODE, DAY, DAY)
    assert len(df) == 2, "半开探针未能恢复"
    stats = data_fetch.session_stats()
    assert stats["breaker_state"] == "closed" and stats["breaker_fails"] == 0
    assert stats["breaker_opens"] == 1


def test_calendar_error_not_counted_as_breaker_failure(stub_login):
    """calendar_error 只触发重建、不计入熔断游程（5 次不算"整日不可用"）。

    走真实 `_fetch_kline_inner`：每次 fetch 内会重试一次，重建受 60s 限流，
    因此 5 次 fetch 后 rebuild_count 仍为 1、breaker 仍 closed。
    """
    ad = _FakeAd(calendar_seq=[TypeError("NoneType"), TypeError("NoneType")])
    stub_login["ad"] = ad
    for _ in range(5):
        with pytest.raises(AdCalendarError):
            data_fetch.fetch_kline(CODE, DAY, DAY)
    stats = data_fetch.session_stats()
    assert stats["breaker_state"] == "closed", "可重建的故障被误判为熔断"
    assert stats["breaker_fails"] == 0
    assert stats["rebuild_count"] == 1, "重建未受限流约束"


# ------------------------------------------------------------ 5 可观测性（心跳）
def test_heartbeat_reports_breaker(tmp_path, monkeypatch):
    """熔断/重建状态必须落 heartbeat.json —— 否则又变成"进程活着 = 一切正常"。"""
    saved_out, saved_err = sys.stdout, sys.stderr
    try:
        import live_run
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err

    monkeypatch.setattr(data_fetch, "REBUILD_MIN_INTERVAL", 0.0)
    data_fetch._rebuild_session("seed")
    for _ in range(5):
        data_fetch._breaker_on_failure(AdSdkError("x", stage="query"))
    assert data_fetch.session_stats()["breaker_state"] == "open"

    path = tmp_path / "heartbeat.json"
    live_run._write_heartbeat(path, datetime.now(), "running", None, 3, 0)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["breaker_state"] == "open"
    assert payload["breaker_opens"] == 1
    assert payload["rebuild_count"] == 1
    assert payload["collect_errors"] == 3
