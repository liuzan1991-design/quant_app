# -*- coding: utf-8 -*-
"""H61 回归：涨跌停价计算 + live 路径真正把 limit_up 喂进撮合。

背景（2026-10-27 核出）：`risk_manager.py:121` 的「涨停禁买」依赖 `MarketSnapshot.limit_up`，
但 live 路径此前恒为 `None`（全仓库只有 `paper_replay` 回放与 `stress_execution` 压测会设）
⇒ 规则存在、从未生效（§6.9 第 11 形态）。

本文件锁 6 件事：
  1. 板块涨跌幅比例（30/68=20%、4/8=30%、主板 10%、ST 5%）
  2. limit_up/down = 前收 ×(1±ratio)，由**上一交易日**收盘推导
  3. 数据首日无前收 ⇒ NaN（不误设限价）
  4. 次新股前 N 日无涨跌幅限制（unlimited_first_days）
  5. **live 接线**：`_process_new_bars` 交给回放引擎的 df 必须带 limit_up（guard-off 必红）
  6. B 项：单标的集中度 15%（风控与监控同源）
"""
from __future__ import annotations

import types

import pandas as pd
import pytest

import live_match_loop
from core import position_cap
from core.price_limit import add_limit_columns, limit_ratio


def _bars(day_closes):
    """按「每天若干根」构造分钟 bar：day_closes = [(date, [close...]), ...]"""
    rows = []
    for day, closes in day_closes:
        for i, c in enumerate(closes):
            rows.append({"time": f"{day} 09:{30 + i:02d}:00", "open": c, "high": c,
                         "low": c, "close": c, "volume": 100, "amount": c * 100})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 1. 比例
@pytest.mark.parametrize("symbol,name,expect", [
    ("300308.SZ", None, 0.20),   # 创业板
    ("301717.SZ", None, 0.20),   # 创业板次新
    ("688256.SH", None, 0.20),   # 科创板
    ("601619.SH", None, 0.10),   # 主板
    ("000001.SZ", None, 0.10),
    ("830799.BJ", None, 0.30),   # 北交所
    ("430047.BJ", None, 0.30),
    ("600519.SH", "ST茅台", 0.05),  # ST 覆盖前缀规则
])
def test_limit_ratio(symbol, name, expect):
    assert limit_ratio(symbol, name) == pytest.approx(expect)


# ---------------------------------------------------------------- 2/3. 限价推导
def test_limit_price_from_prev_day_close():
    df = _bars([("2026-10-26", [100.0, 110.0]), ("2026-10-27", [120.0, 121.0])])
    out = add_limit_columns(df, "300308.SZ")
    day1 = out[out["time"].dt.strftime("%Y-%m-%d") == "2026-10-26"]
    day2 = out[out["time"].dt.strftime("%Y-%m-%d") == "2026-10-27"]
    # 首日无前收 ⇒ 不设限价（避免把"无前收"误算成 0 元）
    assert day1["limit_up"].isna().all() and day1["limit_down"].isna().all()
    # 次日 = 前一日最后一根 close(110) ×(1±20%)
    assert set(day2["limit_up"].dropna().round(2)) == {132.0}
    assert set(day2["limit_down"].dropna().round(2)) == {88.0}


def test_limit_price_main_board_10pct():
    df = _bars([("2026-10-26", [50.0]), ("2026-10-27", [51.0])])
    out = add_limit_columns(df, "601619.SH")
    day2 = out[out["time"].dt.strftime("%Y-%m-%d") == "2026-10-27"]
    assert set(day2["limit_up"].dropna().round(2)) == {55.0}


def test_unlimited_first_days_for_new_listing():
    df = _bars([("2026-08-11", [500.0]), ("2026-08-12", [510.0]), ("2026-08-13", [520.0])])
    out = add_limit_columns(df, "301717.SZ", unlimited_first_days=2)
    early = out[out["time"].dt.strftime("%Y-%m-%d").isin(["2026-08-11", "2026-08-12"])]
    late = out[out["time"].dt.strftime("%Y-%m-%d") == "2026-08-13"]
    assert early["limit_up"].isna().all(), "上市前两日不得设限价"
    assert late["limit_up"].notna().all()


# ---------------------------------------------------------------- 5. live 接线
def test_live_path_feeds_limit_up(monkeypatch):
    """guard-off：删掉 `_process_new_bars` 里那句 `_with_price_limits` 本用例必红。"""
    captured = {}

    class _FakeReplay:
        def __init__(self, broker, store=None):
            self.broker = broker
            self.signal_log = []

        def run(self, df, symbol, handler, autosave_every=0):
            captured["df"] = df.copy()

    monkeypatch.setattr(live_match_loop, "OfflineReplayEngine", _FakeReplay)
    obs = types.SimpleNamespace(
        day_upto={}, broker=object(), store=None, symbol="300308.SZ",
        engine=types.SimpleNamespace(on_bar=lambda *a, **k: None),
        _mark_processed=lambda ts: None, save_watermark=lambda: None,
        signal_path=None, signals=[],
    )
    df = _bars([("2026-10-26", [100.0]), ("2026-10-27", [120.0])])
    live_match_loop._process_new_bars(obs, df)

    fed = captured.get("df")
    assert fed is not None and "limit_up" in fed.columns
    day2 = fed[fed["time"].dt.strftime("%Y-%m-%d") == "2026-10-27"]
    assert day2["limit_up"].notna().all(), "live 必须把 limit_up 交给回放引擎"
    assert set(day2["limit_up"].round(2)) == {120.0}


def test_live_path_survives_limit_failure(monkeypatch):
    """限价算不出来时不得阻断撮合（只降级为"本轮无涨停保护"）。"""
    captured = {}

    class _FakeReplay:
        def __init__(self, broker, store=None):
            self.broker = broker
            self.signal_log = []

        def run(self, df, symbol, handler, autosave_every=0):
            captured["n"] = len(df)

    monkeypatch.setattr(live_match_loop, "OfflineReplayEngine", _FakeReplay)
    monkeypatch.setattr(live_match_loop, "add_limit_columns",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    obs = types.SimpleNamespace(
        day_upto={}, broker=object(), store=None, symbol="300308.SZ",
        engine=types.SimpleNamespace(on_bar=lambda *a, **k: None),
        _mark_processed=lambda ts: None, save_watermark=lambda: None,
        signal_path=None, signals=[],
    )
    live_match_loop._process_new_bars(obs, _bars([("2026-10-27", [100.0])]))
    assert captured.get("n") == 1, "算限价失败仍应把 bar 交给撮合"


# ---------------------------------------------------------------- 6. B 项集中度
def test_symbol_position_cap_is_20pct():
    # 2026-10-28 D2：15% → 20%
    assert live_match_loop._risk().config.max_symbol_position_pct == pytest.approx(0.20)
    assert position_cap.DEFAULT_CAP == pytest.approx(0.20)


def test_position_cap_monitor_uses_risk_config():
    """监控上限必须与风控同源，不能各写一个常量。"""
    snap = position_cap.evaluate(150_000, 1_000_000, shares=1000, cash=0,
                                 cap=live_match_loop._risk().config.max_symbol_position_pct)
    assert snap["cap"] == pytest.approx(0.20)
    assert snap["exceeded"] is False
