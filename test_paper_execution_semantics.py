# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import OrderSide
from core.paper_replay import OfflineReplayEngine, StrategySignal
from core.risk_manager import RiskConfig, RiskManager

OUT = APP_DIR / "test_outputs" / "paper_execution"


def make_broker(slippage=5):
    risk = RiskManager(RiskConfig(max_symbol_position_pct=1, max_account_position_pct=1,
                                  max_order_value_pct=1, max_daily_loss_pct=1,
                                  max_symbol_cumulative_loss_pct=1))
    return PaperBroker(100000, "2026-08-31", risk,
                       PaperBrokerConfig(slippage_bps=slippage, max_volume_participation=1))


def test_next_bar_uses_open_and_single_slippage():
    broker = make_broker(5)
    bars = pd.DataFrame([{"time": "2026-08-31 09:30:00", "open": 10.0,
                          "high": 11.0, "low": 9.8, "close": 10.8, "volume": 100000},
                         {"time": "2026-08-31 09:31:00", "open": 11.0,
                          "high": 12.0, "low": 11.0, "close": 12.0, "volume": 100000}])
    def handler(bar, _):
        if pd.Timestamp(bar["time"]).minute != 30:
            return []
        return [StrategySignal("test", "600000.SH", OrderSide.BUY, 100,
                               pd.Timestamp(bar["time"]).to_pydatetime(), "buy")]
    OfflineReplayEngine(broker).run(bars, "600000.SH", handler)
    assert len(broker.fills) == 1
    assert broker.fills[0].filled_at == datetime(2026, 8, 31, 9, 31)
    assert abs(broker.fills[0].price - 11.0055) < 1e-9


def test_unfilled_sell_proceeds_cannot_finance_buy():
    broker = make_broker(0)
    position = broker.account.positions.setdefault("600000.SH", __import__(
        "core.paper_models", fromlist=["Position"]).Position(
            symbol="600000.SH", quantity=100, sellable_quantity=100,
            average_cost=8.0, market_price=10.0))
    broker.account.cash = 100.0
    bars = pd.DataFrame([{"time": "2026-08-31 09:30:00", "open": 10.0,
                          "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100000}])
    def handler(bar, _):
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        return [StrategySignal("test", "600000.SH", OrderSide.SELL, 100, ts, "sell"),
                StrategySignal("test", "600000.SH", OrderSide.BUY, 100, ts, "rebuy")]
    OfflineReplayEngine(broker).run(bars, "600000.SH", handler)
    assert broker.fills == []
    assert position.quantity == 100
    assert broker.account.cash == 100.0
    buy = next(o for o in broker.orders.values() if o.request.side == OrderSide.BUY)
    assert buy.status.value == "REJECTED"
    assert "资金不足" in buy.reject_reason


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tests = [test_next_bar_uses_open_and_single_slippage,
             test_unfilled_sell_proceeds_cannot_finance_buy]
    lines = []
    for test in tests:
        test()
        lines.append(f"PASS {test.__name__}")
    (OUT / "execution_semantics_result.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
