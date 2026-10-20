# -*- coding: utf-8 -*-
"""H44 硬超时 + 失败分类回归（不登录、不写盘、不触网）。

背景（2026-10-19 D24，35 个失败块）：
- 登录结束后 `finally` 把 `socket.setdefaulttimeout` 恢复成 `None`
  ⇒ `get_calendar()` / `query_kline()` 全程无超时裸奔，实测单轮阻塞 158~397 秒。
- 失败只留未分类 traceback，四类故障（登录超时 / 日历 TypeError /
  空返回 / SDK 异常）需人工看栈分拣。

本文件锁住三件事：
1. **墙钟上限**：整轮超时必须在设定值附近被切断，且异常可辨识（fetch_timeout）。
2. **socket 超时覆盖整个 SDK 段**，且调用结束后复原（不能留下全局副作用）。
3. **四类失败各自带 kind/stage**，日志行单行、可聚合。
"""
from __future__ import annotations

import socket
import threading
import time

import pandas as pd
import pytest

from core.ad_errors import (
    AdCalendarError, AdError, AdFetchTimeout, AdKlineEmpty, AdLoginError,
    AdLoginTimeout, AdSdkError, classify, format_failure_line,
)
from core import data_fetch
from core.market_collector import MarketCollector

CODE = "300308"
DAY = "2026-10-20"
ALL_CLASSES = (AdLoginTimeout, AdLoginError, AdCalendarError,
               AdKlineEmpty, AdSdkError, AdFetchTimeout)


# --------------------------------------------------------------- 假 SDK 构件
class _FakePeriod:
    class min1:
        value = 8


class _FakeConstant:
    Period = _FakePeriod


class _BaseData:
    def __init__(self, behave, recorder=None):
        self.behave = behave
        self.recorder = recorder

    def get_calendar(self):
        if self.recorder is not None:
            self.recorder["socket_timeout"] = socket.getdefaulttimeout()
        return self.behave()


class _MarketData:
    def __init__(self, behavior):
        self.behavior = behavior

    def query_kline(self, codes, **kwargs):
        return self.behavior()


def _ad(base_behave, market_behave, recorder=None):
    class _Ad:
        constant = _FakeConstant

        @staticmethod
        def BaseData():
            return _BaseData(base_behave, recorder)

        @staticmethod
        def MarketData(calendar):
            return _MarketData(market_behave)

    return _Ad()


@pytest.fixture
def stub_login(monkeypatch):
    """装一个假登录；同时桩掉 data_fetch._login，避免「空返回」分支重登打真实 SDK。"""
    state = {"ad": None}
    monkeypatch.setattr(data_fetch, "_login", lambda: state["ad"])
    monkeypatch.setattr(data_fetch, "_AD_CACHE", {})
    return state


def _ok_frame():
    """真实 query_kline 返回的是 {带后缀代码: DataFrame}，替身必须同形。"""
    return {"300308.SZ": pd.DataFrame({
        "time": ["2026-10-20 09:30:00", "2026-10-20 09:31:00"],
        "open": [1.0, 2.0], "high": [1.0, 2.0], "low": [1.0, 2.0],
        "close": [1.0, 2.0], "volume": [100, 200], "amount": [10.0, 20.0],
    })}


# ------------------------------------------------------------------ 1 类语义
@pytest.mark.parametrize("cls,kind", [
    (AdLoginTimeout, "login_timeout"),
    (AdLoginError, "login_error"),
    (AdCalendarError, "calendar_error"),
    (AdKlineEmpty, "kline_empty"),
    (AdSdkError, "sdk_error"),
    (AdFetchTimeout, "fetch_timeout"),
])
def test_each_class_has_stable_kind(cls, kind):
    assert cls("x").kind == kind
    assert cls("x", stage="query").stage == "query"


def test_timeout_classes_keep_timeout_semantics():
    """兼容性：既有代码可能 `except TimeoutError`，新类不得破坏该语义。"""
    for cls in (AdLoginTimeout, AdFetchTimeout):
        assert isinstance(cls("x"), TimeoutError)
    for cls in ALL_CLASSES:
        assert isinstance(cls("x"), RuntimeError)


# ------------------------------------------------------- 2 逐环节分类落点
def test_login_failure_classified(stub_login, monkeypatch):
    def _boom():
        raise AdLoginTimeout("登录超时", stage="login")
    monkeypatch.setattr(data_fetch, "_login", _boom)
    with pytest.raises(AdLoginTimeout) as ei:
        data_fetch.fetch_kline(CODE, DAY, DAY)
    assert ei.value.kind == "login_timeout" and ei.value.stage == "login"


def test_calendar_typeerror_classified(stub_login):
    def _boom():
        raise TypeError("'NoneType' object is not subscriptable")
    stub_login["ad"] = _ad(_boom, lambda: {})
    with pytest.raises(AdCalendarError) as ei:
        data_fetch.fetch_kline(CODE, DAY, DAY)
    assert ei.value.kind == "calendar_error" and ei.value.stage == "calendar"


def test_calendar_none_fails_fast(stub_login):
    """返回 None 不再透传给 MarketData，必须当场归类（否则下游报错更难追）。"""
    stub_login["ad"] = _ad(lambda: None, lambda: {})
    with pytest.raises(AdCalendarError) as ei:
        data_fetch.fetch_kline(CODE, DAY, DAY)
    assert ei.value.kind == "calendar_error"


def test_kline_empty_classified(stub_login):
    stub_login["ad"] = _ad(lambda: object(), lambda: {})
    with pytest.raises(AdKlineEmpty) as ei:
        data_fetch.fetch_kline(CODE, DAY, DAY)
    assert ei.value.kind == "kline_empty" and ei.value.stage == "query"


def test_sdk_exception_classified(stub_login):
    def _boom():
        raise Exception("查询失败")
    stub_login["ad"] = _ad(lambda: object(), _boom)
    with pytest.raises(AdSdkError) as ei:
        data_fetch.fetch_kline(CODE, DAY, DAY)
    assert ei.value.kind == "sdk_error" and ei.value.stage == "query"


def test_happy_path_returns_frame(stub_login):
    stub_login["ad"] = _ad(lambda: object(), _ok_frame)
    df = data_fetch.fetch_kline(CODE, DAY, DAY)
    assert list(df.columns) == data_fetch.STD_COLS and len(df) == 2


# --------------------------------------------------------- 3 socket 超时覆盖
def test_socket_timeout_covers_sdk_and_is_restored(stub_login):
    baseline = socket.getdefaulttimeout()
    rec: dict = {}
    stub_login["ad"] = _ad(lambda: object(), _ok_frame, recorder=rec)
    data_fetch.fetch_kline(CODE, DAY, DAY)
    assert rec["socket_timeout"] == data_fetch._SDK_SOCKET_TIMEOUT, \
        "SDK 调用期间必须处于超时保护下（登录后不得裸奔）"
    assert socket.getdefaulttimeout() == baseline, "调用结束后必须复原全局超时"


def test_socket_timeout_restored_even_on_error(stub_login):
    baseline = socket.getdefaulttimeout()

    def _boom():
        raise Exception("查询失败")
    stub_login["ad"] = _ad(lambda: object(), _boom)
    with pytest.raises(AdSdkError):
        data_fetch.fetch_kline(CODE, DAY, DAY)
    assert socket.getdefaulttimeout() == baseline


def test_socket_timeout_stays_armed_after_hard_timeout(stub_login, monkeypatch):
    """硬超时后**不得**把全局超时复原成 None。

    被放弃的工作线程仍在跑 SDK 调用（Python 无法强杀），若此时复原，它立刻
    回到无限阻塞 —— 等于硬超时只切断了主线程、没切断真正卡住的那条链。
    """
    baseline = socket.getdefaulttimeout()
    blocker = threading.Event()
    stub_login["ad"] = _ad(blocker.wait, lambda: {})
    monkeypatch.setattr(data_fetch, "LIVE_FETCH_TIMEOUT", 0.2)

    box: dict = {}

    def _call():
        try:
            MarketCollector(dry_run=False).fetch_today(CODE, DAY)
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_call, daemon=True)
    t.start()
    t.join(5.0)
    try:
        assert isinstance(box.get("exc"), AdFetchTimeout)
        assert socket.getdefaulttimeout() == data_fetch._SDK_SOCKET_TIMEOUT, \
            "超时后全局 socket 超时被复原 ⇒ 被放弃线程重新裸奔"
    finally:
        socket.setdefaulttimeout(baseline)  # 测试隔离：本文件跑完不留全局副作用


# ------------------------------------------------------------- 4 墙钟硬上限
def test_hard_timeout_cuts_off_blocking_calendar(stub_login, monkeypatch):
    """改前：get_calendar() 永不返回 ⇒ 整轮挂死（D24 的 158s/397s 块）。
    改后：墙钟上限到点即抛 fetch_timeout，不再无限等待。

    用线程 join 而非直接 `pytest.raises`：若上限真被去掉，这里会**失败**而不是
    把整个测试进程挂死 —— 挂死会让"防线失效"看起来像"跑得慢"。
    """
    blocker = threading.Event()  # 永不置位
    stub_login["ad"] = _ad(blocker.wait, lambda: {})
    monkeypatch.setattr(data_fetch, "LIVE_FETCH_TIMEOUT", 0.3)

    collector = MarketCollector(dry_run=False)
    box: dict = {}

    def _call():
        try:
            collector.fetch_today(CODE, DAY)
            box["returned"] = True
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=_call, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(5.0)
    elapsed = time.monotonic() - t0

    assert not t.is_alive(), f"硬超时未生效：调用 {elapsed:.1f}s 后仍未返回"
    exc = box.get("exc")
    assert isinstance(exc, AdFetchTimeout), f"实际异常 {type(exc).__name__}: {exc}"
    assert exc.kind == "fetch_timeout"
    assert elapsed < 2.0, f"切断太慢，耗时 {elapsed:.2f}s"


def test_no_hard_timeout_for_historical_path(stub_login):
    """历史批量补数不传 hard_timeout：不能因为有上限把长区间补数误杀。"""
    stub_login["ad"] = _ad(lambda: object(), _ok_frame)
    df = data_fetch.fetch_kline(CODE, "2020-01-01", "2020-12-31")
    assert len(df) == 2


# --------------------------------------------------------- 5 日志行可聚合
def test_format_failure_line_is_single_line_and_parseable():
    line = format_failure_line(AdCalendarError("boom", stage="calendar"), 158.24)
    assert "\n" not in line, "含换行会破坏「按块计数」"
    assert line.startswith("[collector] 采集失败 class=")
    assert "class=calendar_error" in line
    assert "stage=calendar" in line
    assert line.endswith("elapsed=158.2s")
    assert line.count("采集失败") == 1


def test_classify_falls_back_for_raw_exception():
    kind, stage = classify(TypeError("raw"))
    assert kind == "type_error" and stage == "unknown"
    kind2, stage2 = classify(AdError("x", stage="parse", kind="weird"))
    assert (kind2, stage2) == ("weird", "parse")
