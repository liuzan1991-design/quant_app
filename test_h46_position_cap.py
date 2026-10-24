# -*- coding: utf-8 -*-
"""H46-③：底仓集中度监控（只监控与告警，**不改交易行为**）。

阈值口径：与 `RiskConfig.max_symbol_position_pct = 0.10` 同源。
反例（本次讨论中出现过的两种错误口径）：
- 12%（初版文档）⇒ 比既有上限还宽，等于放行超限；
- 7.56%（"实际 6.3%×1.2"）⇒ 仓位还没到 10% 目标就减仓，方向反了。
实测：现价 612.63、100 股、现金 916,207 ⇒ 7.56% 需 749（+22%）、10% 需 1,018（+66%）、12% 需 1,249（+104%）。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import live_match_loop
from core import position_cap as pc


def test_h46_cap_math_thresholds() -> None:
    got = pc.evaluate(61_263, 977_470, shares=100, cash=916_207)
    assert got["ratio"] == pytest.approx(0.0627, abs=1e-4)
    assert got["exceeded"] is False
    assert got["status"] == "ok"
    assert got["price_for_cap"] == pytest.approx(1018.01, abs=0.05), "10% 需股价 ~1018"


def test_h46_cap_wrong_thresholds_rejected_by_numbers() -> None:
    """12% 与 7.56% 的触发价差一倍以上 —— 说明口径不是小事。"""
    p12 = pc.evaluate(61_263, 977_470, cap=0.12, shares=100, cash=916_207)["price_for_cap"]
    p0756 = pc.evaluate(61_263, 977_470, cap=0.0756, shares=100, cash=916_207)["price_for_cap"]
    assert p12 == pytest.approx(1249.37, abs=0.05)
    assert p0756 == pytest.approx(749.30, abs=0.05)
    assert p12 > p0756 * 1.6


def test_h46_cap_exceeded_and_excess_value() -> None:
    got = pc.evaluate(120_000, 1_000_000, shares=100, cash=880_000)
    assert got["exceeded"] is True and got["status"] == "exceeded"
    assert got["excess_value"] == pytest.approx(20_000), "120k - 10%×1M"


def test_h46_cap_unknown_not_treated_as_ok() -> None:
    for bad in ((None, 1_000_000), (100_000, None), (100_000, 0), (-1, 1_000_000)):
        got = pc.evaluate(*bad)
        assert got["status"] == "unknown" and got["ratio"] is None


def test_h46_summary_contains_position_cap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})
    monkeypatch.setattr(live_match_loop, "_load_live", lambda code: live_match_loop.pd.DataFrame())
    raised, cleared = [], []
    monkeypatch.setattr(live_match_loop, "raise_alert", lambda k, m: raised.append((k, m)))
    monkeypatch.setattr(live_match_loop, "clear_alert", lambda k: cleared.append(k))

    pos = SimpleNamespace(quantity=100, market_price=612.63)
    account = SimpleNamespace(equity=lambda: 977_470.0, cash=916_207.0,
                              positions={"300308.SZ": pos})
    obs = live_match_loop.Observer(
        code="300308", strategy_id="grid_trade", symbol="300308.SZ", params={},
        broker=SimpleNamespace(account=account, orders={}, fills=[]),
        store=SimpleNamespace(root=tmp_path))
    payload = live_match_loop._write_daily_summary(obs)
    assert payload["position_cap"]["ratio"] == pytest.approx(0.0627, abs=1e-4)
    assert payload["position_cap"]["exceeded"] is False
    assert raised == [] and cleared == ["position_cap"], "未超限必须清掉旧告警"


def test_h46_summary_raises_when_exceeded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})
    monkeypatch.setattr(live_match_loop, "_load_live", lambda code: live_match_loop.pd.DataFrame())
    raised = []
    monkeypatch.setattr(live_match_loop, "raise_alert", lambda k, m: raised.append((k, m)))
    monkeypatch.setattr(live_match_loop, "clear_alert", lambda k: None)

    pos = SimpleNamespace(quantity=100, market_price=1_100.0)   # 110k / 权益 ~1.0M ⇒ 11%
    account = SimpleNamespace(equity=lambda: 1_000_000.0, cash=890_000.0,
                              positions={"300308.SZ": pos})
    obs = live_match_loop.Observer(
        code="300308", strategy_id="grid_trade", symbol="300308.SZ", params={},
        broker=SimpleNamespace(account=account, orders={}, fills=[]),
        store=SimpleNamespace(root=tmp_path))
    payload = live_match_loop._write_daily_summary(obs)
    assert payload["position_cap"]["exceeded"] is True
    assert raised and raised[0][0] == "position_cap"
    assert "仅告警" in raised[0][1]
