# -*- coding: utf-8 -*-
"""H9 风控验收：行情时钟、T+1、续成风控、止损计数和配置上限。"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import MarketSnapshot, OrderRequest, OrderSide, Position
from core.risk_manager import RiskConfig, RiskManager

NOW = datetime(2026, 9, 18, 10, 0, 0)
SYMBOL = "300308.SZ"


def _req(oid: str, side: OrderSide, qty: int) -> OrderRequest:
    return OrderRequest(client_order_id=oid, symbol=SYMBOL, side=side,
                        quantity=qty, created_at=NOW, strategy_id="h9")


def _snap(price: float = 100.0, volume: int = 10_000_000,
          ts: datetime = NOW) -> MarketSnapshot:
    return MarketSnapshot(symbol=SYMBOL, timestamp=ts, last_price=price, volume=volume)


def _loose_risk() -> RiskManager:
    """放宽所有仓位/金额门，用于隔离观察单一规则。"""
    return RiskManager(RiskConfig(max_symbol_position_pct=1.0,
                                  max_account_position_pct=1.0,
                                  max_order_value_pct=1.0,
                                  max_daily_loss_pct=1.0,
                                  max_symbol_cumulative_loss_pct=1.0))


def test_p0_1_offline_replay_uses_logical_clock():
    """离线回放可以用历史逻辑时钟，实时时钟的验收见 execution_risk_accounting。"""
    ts = NOW
    broker = PaperBroker(1_000_000, "2026-09-18")
    snapshot = _snap(ts=ts)
    # 离线调用以历史时点作为 now，不拿当前日期拦截整段历史行情。
    delay = max(0.0, (ts - snapshot.timestamp).total_seconds())
    assert delay == 0.0

    order = broker.submit_order(_req("h9-p1-fresh", OrderSide.BUY, 100), snapshot, ts)
    assert order.status.value == "FILLED", "延迟 0 时不应被 stale_quote 拦下"


def test_p0_1_rule_itself_is_alive():
    """P0-1 对照：规则本身有效——真陈旧行情确实会被拦（证明是"契约"问题而非"规则"问题）。"""
    broker = PaperBroker(1_000_000, "2026-09-18")
    stale = _snap(ts=NOW - timedelta(seconds=600))
    order = broker.submit_order(_req("h9-p1-stale", OrderSide.BUY, 100), stale, NOW)
    assert order.status.value == "REJECTED"
    assert "行情延迟" in order.reject_reason


def test_p0_2_risk_and_match_agree_on_sellable_quantity():
    """H77：今买不再从已排除今日买入的可卖量中重复扣除。"""
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=_loose_risk(),
                         config=PaperBrokerConfig(slippage_bps=0, max_volume_participation=1.0))
    broker.account.positions[SYMBOL] = Position(
        symbol=SYMBOL, quantity=1000, sellable_quantity=1000,
        average_cost=100.0, market_price=100.0, today_bought_quantity=0)
    broker.account.cash = 900_000.0

    broker.submit_order(_req("h9-p2-buy", OrderSide.BUY, 100), _snap(), NOW)
    pos = broker.account.positions[SYMBOL]
    assert (pos.quantity, pos.sellable_quantity, pos.today_bought_quantity) == (1100, 1000, 100)

    risk_view = pos.sellable_quantity                                   # risk_manager.py:93
    match_view = max(0, min(pos.sellable_quantity, pos.quantity - pos.today_bought_quantity))
    assert risk_view == match_view == 1000

    sell = broker.submit_order(_req("h9-p2-sell", OrderSide.SELL, 1000), _snap(), NOW)
    assert sell.status.value == "FILLED"
    assert sell.filled_quantity == 1000, "昨日 1000 股应全部可卖，当日新买 100 股仍保留"
    assert (pos.quantity, pos.sellable_quantity, pos.today_bought_quantity) == (100, 0, 100)


def test_p0_3_partial_fill_continuation_rechecks_risk():
    """亏损熔断后撤销旧买单余量，持仓仍可合法减仓。"""
    broker = PaperBroker(1_000_000, "2026-09-18",
                         risk_manager=RiskManager(RiskConfig(max_symbol_position_pct=1.0,
                                                            max_account_position_pct=1.0,
                                                            max_order_value_pct=1.0)),
                         config=PaperBrokerConfig(slippage_bps=0, max_volume_participation=0.10))
    first = broker.submit_order(_req("h9-p3", OrderSide.BUY, 1000), _snap(volume=1000), NOW)
    assert first.status.value == "PARTIALLY_FILLED"
    filled_before = first.filled_quantity

    # 账户已远超单日亏损停机线
    broker.account.daily_realized_pnl = -500_000.0

    # 注意：first 与返回的 order 是同一对象，必须先取 filled_quantity 快照再比较
    continued = broker.match_order("h9-p3", _snap(volume=10_000_000), NOW + timedelta(minutes=1))
    assert continued.filled_quantity == filled_before
    assert continued.status.value == "CANCELLED"
    assert not list(broker.pending_orders())
    next_day = NOW + timedelta(days=3)
    broker.start_new_trading_day(next_day.date().isoformat())
    broker.account.daily_realized_pnl = -500_000.0
    reducing = broker.submit_order(_req("h9-reduce", OrderSide.SELL, 100),
                                   _snap(ts=next_day), next_day)
    assert reducing.status.value == "FILLED"


def test_p0_4_actual_stop_loss_partial_fills_count_once():
    from dataclasses import replace

    risk = _loose_risk()
    risk.config = replace(risk.config, stop_loss_streak_limit=1)
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=risk,
                         config=PaperBrokerConfig(slippage_bps=0))
    broker.account.positions[SYMBOL] = Position(
        symbol=SYMBOL, quantity=200, sellable_quantity=200,
        average_cost=110.0, market_price=100.0)
    req = replace(_req("stop", OrderSide.SELL, 200), reason="T仓位止损")
    order = broker.submit_order(req, _snap(volume=1000), NOW)
    assert order.status.value == "PARTIALLY_FILLED"
    assert broker.account.consecutive_stop_losses[SYMBOL] == 1
    later = NOW + timedelta(minutes=1)
    broker.match_order("stop", _snap(volume=1000, ts=later), later)
    assert order.status.value == "FILLED"
    assert broker.account.consecutive_stop_losses[SYMBOL] == 1
    buy = broker.submit_order(_req("reentry", OrderSide.BUY, 100), _snap(ts=later), later)
    assert "暂停至" in buy.reject_reason


def test_p0_4_gate_works_when_manually_invoked():
    """P0-4 对照：门本身可用——手工累计 3 次止损后确实被拦。"""
    broker = PaperBroker(1_000_000, "2026-09-18",
                         risk_manager=RiskManager(RiskConfig(stop_loss_streak_limit=3,
                                                            pause_trading_days=2,
                                                            max_symbol_position_pct=1.0,
                                                            max_account_position_pct=1.0,
                                                            max_order_value_pct=1.0)))
    for _ in range(3):
        broker.risk_manager.record_stop_loss(broker.account, SYMBOL, NOW.date())
    blocked = broker.submit_order(_req("h9-p4", OrderSide.BUY, 100), _snap(), NOW)
    assert blocked.status.value == "REJECTED"
    assert "暂停至" in blocked.reject_reason


def test_p0_5_position_gate_blocks_grid_add_on():
    """P0-5：建底仓已占 9.18%，10% 单股硬门使网格再买 100 股即被拒（敞口被顶死）。

    注意本用例固化的是「观察期 live 风控参数」下的行为：
    max_symbol_position_pct=0.10 / max_order_value_pct=0.10（live_match_loop.py:54）。
    """
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.10,
                                  max_account_position_pct=0.90,
                                  max_order_value_pct=0.10))
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=risk,
                         config=PaperBrokerConfig(slippage_bps=5, max_volume_participation=0.10))
    # D8 真实持仓快照
    broker.account.positions[SYMBOL] = Position(
        symbol=SYMBOL, quantity=100, sellable_quantity=100,
        average_cost=896.0, market_price=926.43, today_bought_quantity=0)
    broker.account.cash = 916_087.0

    decision = risk.evaluate(_req("h9-p5", OrderSide.BUY, 100), broker.account,
                             _snap(price=926.43), NOW)
    assert not decision.passed, "10% 硬门下再买 100 股应被拒（缺陷本身）"
    codes = {v.code for v in decision.violations}
    assert "symbol_position_limit" in codes


def test_p0_5_ma_swing_first_signal_blocked_above_threshold():
    """P0-5：ma_swing 空仓时，价格 >1008 的首个信号也会被 10% 单笔门拒绝。"""
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.10,
                                  max_account_position_pct=0.90,
                                  max_order_value_pct=0.10))
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=risk)

    ok = risk.evaluate(_req("h9-p5-ma-ok", OrderSide.BUY, 100), broker.account,
                       _snap(price=1000.0), NOW)
    assert ok.passed, "1000 元时 100 股恰好 10.00%，应通过（边界）"

    blocked = risk.evaluate(_req("h9-p5-ma-blocked", OrderSide.BUY, 100), broker.account,
                            _snap(price=1010.0), NOW)
    assert not blocked.passed, "1010 元时 100 股超 10%，应被拒（缺陷本身）"


if __name__ == "__main__":
    tests = [
        test_p0_1_offline_replay_uses_logical_clock,
        test_p0_1_rule_itself_is_alive,
        test_p0_2_risk_and_match_agree_on_sellable_quantity,
        test_p0_3_partial_fill_continuation_rechecks_risk,
        test_p0_4_actual_stop_loss_partial_fills_count_once,
        test_p0_4_gate_works_when_manually_invoked,
        test_p0_5_position_gate_blocks_grid_add_on,
        test_p0_5_ma_swing_first_signal_blocked_above_threshold,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\nH9 风控验收：{len(tests)} 项通过")
