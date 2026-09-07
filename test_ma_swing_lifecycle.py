# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.ma_swing_live import MaSwingLiveEngine
from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import Fill, OrderSide
from core.risk_manager import RiskConfig, RiskManager
from strategies.ma_swing import MaSwingStrategy


def broker(cash=1_000_000):
    risk = RiskManager(RiskConfig(
        max_symbol_position_pct=1,
        max_account_position_pct=1,
        max_order_value_pct=1,
        max_daily_loss_pct=1,
        max_symbol_cumulative_loss_pct=1,
    ))
    return PaperBroker(cash, "2026-01-05", risk, PaperBrokerConfig(max_volume_participation=1))


def fill(order_id, symbol, side, quantity, price, ts):
    return Fill(
        fill_id=f"fill-{order_id}", client_order_id=order_id, symbol=symbol,
        side=side, quantity=quantity, price=price, fee=0.0, filled_at=ts,
    )


def test_buy_state_updates_only_after_fill():
    symbol = "300308.SZ"
    engine = MaSwingLiveEngine(symbol, MaSwingStrategy.default_params())
    b = broker()
    assert engine.state.entry_day is None
    b.fills.append(fill("b1", symbol, OrderSide.BUY, 100, 10.0,
                        datetime(2026, 1, 6, 9, 31)))
    engine._sync_lifecycle_from_fills(b)
    assert engine.state.entry_day.isoformat() == "2026-01-06"
    assert engine.state.highest_price == 10.0


def test_rejected_or_unfilled_sell_keeps_state():
    symbol = "300308.SZ"
    engine = MaSwingLiveEngine(symbol, MaSwingStrategy.default_params())
    b = broker()
    engine.state.entry_day = pd.Timestamp("2026-01-05").date()
    engine.state.highest_price = 12.0
    engine._sync_lifecycle_from_fills(b)
    assert engine.state.entry_day.isoformat() == "2026-01-05"
    assert engine.state.highest_price == 12.0


def test_partial_sell_keeps_state_until_flat():
    symbol = "300308.SZ"
    engine = MaSwingLiveEngine(symbol, MaSwingStrategy.default_params())
    b = broker()
    position = b.account.positions.setdefault(symbol, __import__("core.paper_models", fromlist=["Position"]).Position(symbol=symbol))
    position.quantity = 200
    engine.state.entry_day = pd.Timestamp("2026-01-05").date()
    engine.state.highest_price = 12.0
    b.fills.append(fill("s1", symbol, OrderSide.SELL, 100, 11.0,
                        datetime(2026, 1, 7, 9, 31)))
    engine._sync_lifecycle_from_fills(b)
    assert engine.state.entry_day.isoformat() == "2026-01-05"
    position.quantity = 0
    b.fills.append(fill("s2", symbol, OrderSide.SELL, 100, 10.5,
                        datetime(2026, 1, 7, 9, 32)))
    engine._sync_lifecycle_from_fills(b)
    assert engine.state.entry_day is None
    assert engine.state.highest_price == 0.0


def test_same_bar_sell_buy_uses_fill_sequence():
    symbol = "300308.SZ"
    engine = MaSwingLiveEngine(symbol, MaSwingStrategy.default_params())
    b = broker()
    position = b.account.positions.setdefault(symbol, __import__("core.paper_models", fromlist=["Position"]).Position(symbol=symbol))
    engine.state.entry_day = pd.Timestamp("2026-01-05").date()
    engine.state.highest_price = 12.0
    position.quantity = 0
    ts = datetime(2026, 1, 8, 9, 30)
    b.fills.append(fill("s1", symbol, OrderSide.SELL, 100, 11.0, ts))
    engine._sync_lifecycle_from_fills(b)
    assert engine.state.entry_day is None
    position.quantity = 100
    b.fills.append(fill("b1", symbol, OrderSide.BUY, 100, 11.1, ts))
    engine._sync_lifecycle_from_fills(b)
    assert engine.state.entry_day.isoformat() == "2026-01-08"
    assert engine.state.highest_price == 11.1


def test_holding_period_starts_at_actual_fill_day():
    symbol = "300308.SZ"
    engine = MaSwingLiveEngine(symbol, MaSwingStrategy.default_params())
    b = broker()
    b.fills.append(fill("b1", symbol, OrderSide.BUY, 100, 10.0,
                        datetime(2026, 1, 9, 10, 15)))
    engine._sync_lifecycle_from_fills(b)
    assert engine.state.entry_day.isoformat() == "2026-01-09"


if __name__ == "__main__":
    tests = [
        test_buy_state_updates_only_after_fill,
        test_rejected_or_unfilled_sell_keeps_state,
        test_partial_sell_keeps_state_until_flat,
        test_same_bar_sell_buy_uses_fill_sequence,
        test_holding_period_starts_at_actual_fill_day,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
