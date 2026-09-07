# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import (
    MarketSnapshot,
    OrderRequest,
    OrderSide,
    OrderStatus,
    Position,
)
from core.risk_manager import RiskConfig, RiskManager


NOW = datetime(2026, 8, 31, 10, 0, 0)


def request(order_id: str, side: OrderSide, quantity: int = 100,
            symbol: str = "600000.SH") -> OrderRequest:
    return OrderRequest(client_order_id=order_id, symbol=symbol, side=side,
                        quantity=quantity, created_at=NOW, strategy_id="test")


def market(price: float = 10.0, volume: int = 100000,
           timestamp: datetime = NOW, **kwargs) -> MarketSnapshot:
    return MarketSnapshot(symbol="600000.SH", timestamp=timestamp,
                          last_price=price, volume=volume, **kwargs)


def test_stale_quote_rejected():
    broker = PaperBroker(100000, "2026-08-31")
    order = broker.submit_order(request("stale", OrderSide.BUY),
                                market(timestamp=NOW - timedelta(seconds=121)), NOW)
    assert order.status == OrderStatus.REJECTED
    assert "行情延迟" in order.reject_reason


def test_position_limits_and_daily_stop():
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.10,
                                  max_account_position_pct=0.60,
                                  max_order_value_pct=0.20))
    broker = PaperBroker(100000, "2026-08-31", risk_manager=risk)
    too_large = broker.submit_order(request("large", OrderSide.BUY, 1100), market(), NOW)
    assert too_large.status == OrderStatus.REJECTED
    assert "单股仓位" in too_large.reject_reason

    broker.account.daily_realized_pnl = -1000
    stopped = broker.submit_order(request("daily-stop", OrderSide.BUY, 100), market(), NOW)
    assert stopped.status == OrderStatus.REJECTED
    assert "单日亏损" in stopped.reject_reason


def test_idempotent_order_and_partial_fill():
    config = PaperBrokerConfig(max_volume_participation=0.10, slippage_bps=0)
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.50,
                                  max_account_position_pct=0.70,
                                  max_order_value_pct=0.50))
    broker = PaperBroker(100000, "2026-08-31", risk_manager=risk, config=config)
    req = request("same-id", OrderSide.BUY, 1000)
    first = broker.submit_order(req, market(volume=2000), NOW)
    assert first.status == OrderStatus.PARTIALLY_FILLED
    assert first.filled_quantity == 200
    cash_after_first = broker.account.cash

    replay = broker.submit_order(req, market(volume=2000), NOW)
    assert replay is first
    assert broker.account.cash == cash_after_first
    assert len(broker.fills) == 1

    broker.match_order("same-id", market(volume=10000), NOW + timedelta(minutes=1))
    assert first.status == OrderStatus.FILLED
    assert first.filled_quantity == 1000


def test_t_plus_one_and_new_day_unlock():
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.50,
                                  max_account_position_pct=0.70,
                                  max_order_value_pct=0.50))
    broker = PaperBroker(100000, "2026-08-31", risk_manager=risk,
                         config=PaperBrokerConfig(slippage_bps=0))
    buy = broker.submit_order(request("buy", OrderSide.BUY, 1000), market(), NOW)
    assert buy.status == OrderStatus.FILLED
    position = broker.account.positions["600000.SH"]
    assert position.quantity == 1000
    assert position.sellable_quantity == 0

    sell_today = broker.submit_order(request("sell-today", OrderSide.SELL, 100), market(), NOW)
    assert sell_today.status == OrderStatus.REJECTED
    assert "T+1" in sell_today.reject_reason

    broker.start_new_trading_day("2026-09-01")
    sell_next_day = broker.submit_order(request("sell-next", OrderSide.SELL, 100),
                                        market(timestamp=NOW + timedelta(days=1)),
                                        NOW + timedelta(days=1))
    assert sell_next_day.status == OrderStatus.FILLED
    assert position.quantity == 900
    assert position.sellable_quantity == 900


def test_loss_pause_and_limit_states():
    risk = RiskManager()
    broker = PaperBroker(100000, "2026-08-31", risk_manager=risk)
    broker.account.positions["600000.SH"] = Position(
        symbol="600000.SH", quantity=1000, sellable_quantity=1000,
        average_cost=10, market_price=10,
    )
    broker.account.realized_pnl_by_symbol["600000.SH"] = -5000
    order = broker.submit_order(request("symbol-stop", OrderSide.SELL, 100), market(), NOW)
    assert order.status == OrderStatus.REJECTED
    assert "累计亏损" in order.reject_reason

    broker2 = PaperBroker(100000, "2026-08-31")
    up = broker2.submit_order(request("up", OrderSide.BUY, 100),
                              market(price=11, limit_up=11), NOW)
    assert up.status == OrderStatus.REJECTED
    assert "涨停" in up.reject_reason


def test_stop_loss_streak_pause():
    risk = RiskManager(RiskConfig(stop_loss_streak_limit=3, pause_trading_days=2))
    broker = PaperBroker(100000, "2026-08-31", risk_manager=risk)
    for _ in range(3):
        risk.record_stop_loss(broker.account, "600000.SH", NOW.date())
    blocked = broker.submit_order(request("paused", OrderSide.BUY, 100), market(), NOW)
    assert blocked.status == OrderStatus.REJECTED
    assert "暂停至" in blocked.reject_reason


def test_terminal_state_cannot_transition():
    broker = PaperBroker(100000, "2026-08-31")
    order = broker.submit_order(request("filled", OrderSide.BUY, 100), market(), NOW)
    assert order.status == OrderStatus.FILLED
    try:
        broker.cancel_order("filled", NOW)
    except ValueError as exc:
        assert "终态订单" in str(exc)
    else:
        raise AssertionError("终态订单不应允许撤单")


def main():
    tests = [
        test_stale_quote_rejected,
        test_position_limits_and_daily_stop,
        test_idempotent_order_and_partial_fill,
        test_t_plus_one_and_new_day_unlock,
        test_loss_pause_and_limit_states,
        test_stop_loss_streak_pause,
        test_terminal_state_cannot_transition,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"P0模拟盘安全层测试通过：{len(tests)}项")


if __name__ == "__main__":
    main()
