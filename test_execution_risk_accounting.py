# -*- coding: utf-8 -*-
"""将整体审查中的反例变成成交、资金与收益的正向验收。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from core import metrics, performance
from core.cross_risk import CrossSymbolLedger, PortfolioGate
from core.grid_shared_live import GridSharedLiveEngine
from core.intraday_t_shared_live import IntradayTSharedLiveEngine
from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import MarketSnapshot, OrderRequest, OrderSide, OrderStatus
from core.paper_replay import OfflineReplayEngine, StrategySignal
from core.paper_store import PaperStateStore
from core.risk_manager import RiskConfig, RiskManager
from core.stress_execution import StressExecutionEngine
from strategies.base import BacktestResult

SYMBOL = "301717.SZ"
T0 = datetime(2026, 11, 2, 9, 30)


def broker(day="2026-11-02"):
    risk = RiskManager(RiskConfig(max_order_value_pct=1, max_symbol_position_pct=1,
                                  max_account_position_pct=1))
    return PaperBroker(300000, day, risk, PaperBrokerConfig(
        slippage_bps=0, commission_rate=0, min_commission=0, transfer_fee_rate=0, stamp_tax_rate=0))


def rows(*prices, volume=10000):
    return pd.DataFrame([{"time": T0 + timedelta(minutes=i), "open": op, "close": cl,
                          "high": max(op, cl), "low": min(op, cl), "volume": volume,
                          "amount": cl * volume}
                         for i, (op, cl) in enumerate(prices)])


def signal(bar, *, strategy="test", side=OrderSide.BUY, qty=100):
    return StrategySignal(strategy, SYMBOL, side, qty, bar["time"].to_pydatetime(), "test")


def intent(oid, ts=T0, qty=100, side=OrderSide.BUY):
    return OrderRequest(oid, SYMBOL, side, qty, ts)


def test_causal_fill_same_after_split_and_snapshot(tmp_path):
    bars = rows((100, 100), (90, 110), (103, 104))

    def handler(bar, _):
        return [signal(bar)] if bar["close"] > 105 else []

    full = OfflineReplayEngine(broker()).run(bars, SYMBOL, handler)
    store = PaperStateStore(tmp_path)
    part = OfflineReplayEngine(broker(), store).run(bars.iloc[:2], SYMBOL, handler)
    assert part.fills == []
    assert next(iter(part.orders.values())).status == OrderStatus.SUBMITTED
    resumed = OfflineReplayEngine(store.load()).run(bars.iloc[1:], SYMBOL, handler)
    for account in (full, resumed):
        assert [(f.filled_at, f.price, f.quantity) for f in account.fills] == [
            (T0 + timedelta(minutes=2), 103.0, 100)]
        assert account.account.positions[SYMBOL].market_price == 104
    assert full.account.cash == resumed.account.cash


def test_delayed_tail_never_backfills_and_wait_survives_restart(tmp_path):
    bars = rows((10, 12), (11, 12), (12, 13), (13, 14))
    store = PaperStateStore(tmp_path)
    handler = lambda bar, _: [signal(bar)] if bar["time"] == T0 else []
    partial = StressExecutionEngine(broker(), store, latency_bars=3).run(bars.iloc[:1], SYMBOL, handler)
    assert not partial.fills
    resumed = StressExecutionEngine(store.load(), latency_bars=3).run(bars.iloc[1:3], SYMBOL, handler)
    assert not resumed.fills
    StressExecutionEngine(resumed, latency_bars=3).run(bars.iloc[3:], SYMBOL, handler)
    assert [(f.filled_at, f.price) for f in resumed.fills] == [(T0 + timedelta(minutes=3), 13)]


def test_legacy_reference_keeps_explicit_same_bar_fill():
    bars = rows((100, 110))
    replay = OfflineReplayEngine(broker(), execution_mode="legacy_reference")
    replay.run(bars, SYMBOL, lambda bar, _: [signal(bar)])
    assert [(f.filled_at, f.price) for f in replay.broker.fills] == [(T0, 100)]


@pytest.mark.parametrize("volume", [0, float("nan")])
def test_zero_or_unknown_volume_cannot_fill(volume):
    b = broker()
    bars = rows((100, 100), (100, 100))
    bars.loc[1, "volume"] = volume
    OfflineReplayEngine(b).run(bars, SYMBOL, lambda bar, _: [signal(bar)] if bar["time"] == T0 else [])
    assert not b.fills
    assert next(iter(b.orders.values())).status == OrderStatus.SUBMITTED


def test_shared_minute_capacity_and_restart(tmp_path):
    b = broker()
    quote = MarketSnapshot(SYMBOL, T0, 100, 1000)
    b.submit_order(intent("first"), quote, T0)
    b.submit_order(intent("second"), quote, T0)
    store = PaperStateStore(tmp_path)
    store.save(b)
    restored = store.load()
    restored.match_order("second", quote, T0)
    assert sum(fill.quantity for fill in restored.fills) == 100
    assert restored.orders["second"].status == OrderStatus.SUBMITTED
    later = T0 + timedelta(minutes=1)
    restored.match_order("second", MarketSnapshot(SYMBOL, later, 100, 1000), later)
    assert restored.orders["second"].status == OrderStatus.FILLED


def test_cross_cap_reserves_pending_and_releases_cancelled():
    first, second = broker(), broker()
    ledger = CrossSymbolLedger(.25, 300000)
    gate1, gate2 = PortfolioGate(ledger, "one", first), PortfolioGate(ledger, "two", second)
    a = rows((400, 400), volume=0)
    handler = lambda bar, _: [signal(bar)]
    OfflineReplayEngine(first, gate=gate1).run(a, SYMBOL, handler)
    blocked = OfflineReplayEngine(second, gate=gate2)
    blocked.run(a, SYMBOL, handler)
    assert first.fills == second.fills == []
    assert blocked.signal_log[0]["order_status"] == "REJECTED"
    assert not second.orders
    later = T0 + timedelta(minutes=1)
    first.cancel_order(next(iter(first.orders)), later)
    assert gate2(signal(a.iloc[0]), 400, 0)


def test_gap_rechecks_cross_cap_before_pending_buy_fills():
    b = broker()
    gate = PortfolioGate(CrossSymbolLedger(.25, 300000), "one", b)
    bars = rows((400, 400), (800, 800))
    OfflineReplayEngine(b, gate=gate).run(
        bars, SYMBOL, lambda bar, _: [signal(bar)] if bar["time"] == T0 else [])
    assert not b.fills
    assert next(iter(b.orders.values())).status == OrderStatus.CANCELLED
    assert gate.ledger.rejects[-1]["reason"] == "跨策略单标上限"


def test_live_loop_orders_all_open_fills_before_close_and_shares_volume(tmp_path, monkeypatch):
    import live_match_loop as live

    cfg = SimpleNamespace(initial_cash=300000, cross_symbol_cap=.25, risk={})
    first, second = broker(), broker()
    bars = rows((100, 100), (100, 100), volume=1000)
    seen = []
    monkeypatch.setattr(live, "_load_live", lambda _: bars)
    monkeypatch.setattr(live, "_with_price_limits", lambda full, new, symbol: new)
    observers = []
    for name, b in (("one", first), ("two", second)):
        def handler(bar, account, strategy=name):
            if bar["time"] == T0:
                return [signal(bar, strategy=strategy)]
            seen.append((strategy, len(first.fills), len(second.fills)))
            return []

        observers.append(live.Observer("301717", name, SYMBOL, {}, broker=b, cfg=cfg,
                                       engine=SimpleNamespace(on_bar=handler),
                                       store=PaperStateStore(tmp_path / name)))
    live._run_once(observers, tmp_path, quote_now=T0 + timedelta(minutes=1))
    assert seen == []  # 第二根尚未完成，不能提前吃到成交量和收盘价。
    assert not first.fills and not second.fills
    live._run_once(observers, tmp_path, quote_now=T0 + timedelta(minutes=2))
    assert seen == [("one", 1, 0), ("two", 1, 0)]
    assert len(first.fills) + len(second.fills) == 1
    assert next(iter(second.orders.values())).status == OrderStatus.SUBMITTED


def test_stale_live_quotes_block_new_and_pending_orders():
    b = broker()
    snap = MarketSnapshot(SYMBOL, T0, 100, 10000)
    b.submit_order(intent("pending"), snap, T0, execute_immediately=False)
    bars = rows((100, 100), (100, 100))
    replay = OfflineReplayEngine(b, quote_now=T0 + timedelta(minutes=15))
    replay.run(bars, SYMBOL, lambda bar, _: [signal(bar)])
    assert not b.fills
    assert all(s["order_status"] == "REJECTED" for s in replay.signal_log)
    assert any("stale_quote" in event.get("violations", []) for event in b.audit_log)


@pytest.mark.parametrize("bonus, expected_base, expected_t, expected_cost", [
    (0, 100, 100, 119), (1, 200, 200, 59.5)])
def test_t_inventory_cost_applies_dividend_and_bonus_after_restart(
        tmp_path, bonus, expected_base, expected_t, expected_cost):
    b = broker("2026-10-30")
    bought = datetime(2026, 10, 30, 10)
    from dataclasses import replace
    b.submit_order(replace(intent("base", bought), reason="建底仓"),
                   MarketSnapshot(SYMBOL, bought, 100, 10000), bought)
    b.submit_order(replace(intent("t", bought), reason="加权买入[试算] 评分=10"),
                   MarketSnapshot(SYMBOL, bought, 120, 10000), bought)
    action = SimpleNamespace(ex_date="2026-11-02", dividend_per_share=1,
                             bonus_ratio=bonus, plan_desc="分红送转")
    assert b.apply_corporate_action(action, SYMBOL)
    store = PaperStateStore(tmp_path)
    store.save(b)
    restored = store.load()
    assert not restored.apply_corporate_action(action, SYMBOL)
    engine = IntradayTSharedLiveEngine(SYMBOL, {"base_position": 100}, broker_driven=True)
    engine._sync_lifecycle_from_fills(restored)
    assert engine.account.base_position == expected_base
    assert engine.engine.state.t_total_shares == expected_t
    assert engine.engine.state.t_avg_cost == expected_cost


def test_grid_restore_uses_broker_inventory_to_reduce(tmp_path):
    b = broker("2026-10-30")
    t = datetime(2026, 10, 30, 10)
    b.submit_order(intent("held", t, qty=200), MarketSnapshot(SYMBOL, t, 100, 10000), t)
    store = PaperStateStore(tmp_path)
    store.save(b)
    restored = store.load()
    engine = GridSharedLiveEngine(SYMBOL, {"base_position": 100, "trade_shares": 100},
                                  broker_driven=True)
    engine._prepared = True
    engine.engine.state.anchor_price = 100
    engine.engine.state.grid_bought = 5  # 旧影子状态，应由实仓纠正
    bars = rows((80, 80), (80, 80))
    replay = OfflineReplayEngine(restored)
    replay.run(bars, SYMBOL, engine.on_bar)
    assert replay.signal_log[0]["reason"] == "网格硬止损"
    assert restored.account.positions[SYMBOL].quantity == 100
    assert engine.engine.state.grid_bought == 0
    assert engine.engine._init_cash == 300000


def test_period_returns_include_initial_capital():
    times = pd.to_datetime(["2026-10-30 15:00", "2026-11-02 15:00"])
    equity = pd.Series([99000, 110000], index=times)
    summary = performance.summarize(equity, initial_cash=100000)
    assert summary["total_return"] == pytest.approx(.1)
    assert summary["max_drawdown"] == pytest.approx(-.01)
    assert performance.daily_returns(equity, initial_cash=100000).iloc[0] == pytest.approx(-.01)
    result = BacktestResult("demo", "demo", {}, 100000,
                            equity.rename("equity").rename_axis("time").reset_index(), pd.DataFrame())
    month = metrics.monthly_returns(result)
    assert month.loc["2026", 10] == pytest.approx(-1)
    assert month.loc["2026", 11] == pytest.approx((110000 / 99000 - 1) * 100)


def test_unrealized_trade_and_matching_benchmark():
    bars = rows((100, 100), (90, 90), (95, 95))
    trades = pd.DataFrame([
        {"time": bars.time[0], "direction": "BUY", "price": 100, "shares": 100, "reason": "建底仓", "fee": 1},
        {"time": bars.time[1], "direction": "BUY", "price": 90, "shares": 100, "reason": "加权买入", "fee": 1}])
    result = BacktestResult("demo", "demo", {}, 100000, bars, trades,
                            base_price=100, base_shares=100, final_equity=99998, total_fees=2)
    attr = metrics.attribution(result, bars, pd.DataFrame())
    assert attr["底仓收益"] == -500
    assert attr["未实现T盈亏"] == 500
    assert attr["合计"] == -2
    assert attr["对账通过"]
    passive = rows((100, 100), (120, 120))
    holding = BacktestResult("demo", "demo", {}, 1000000, passive, pd.DataFrame(),
                             base_price=100, base_shares=3000, total_return=.06)
    compare = metrics.benchmark_comparison(holding, passive)
    assert compare["增量收益%"] == pytest.approx(0)
    assert compare["全仓机会成本差%"] == pytest.approx(-14)
