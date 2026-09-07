# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import OrderSide, OrderStatus
from core.paper_replay import OfflineReplayEngine, SignalOrderAdapter, StrategySignal
from core.paper_store import PaperStateStore
from core.risk_manager import RiskConfig, RiskManager

OUTPUT_DIR = APP_DIR / "test_outputs" / "paper_replay"


def test_signal_id_is_deterministic():
    signal = StrategySignal("demo", "600000.SH", OrderSide.BUY, 100,
                            datetime(2026, 8, 31, 9, 31), "首次买入")
    first = SignalOrderAdapter.to_order(signal)
    second = SignalOrderAdapter.to_order(signal)
    assert first.client_order_id == second.client_order_id


def test_replay_persistence_and_recovery():
    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    store = PaperStateStore(OUTPUT_DIR)
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.50,
                                  max_account_position_pct=0.70,
                                  max_order_value_pct=0.50))
    broker = PaperBroker(100000, "2026-08-31", risk_manager=risk,
                         config=PaperBrokerConfig(slippage_bps=0,
                                                  max_volume_participation=0.10))
    bars = pd.DataFrame([
        {"time": "2026-08-31 09:31:00", "close": 10.0, "volume": 1000},
        {"time": "2026-08-31 09:32:00", "close": 10.1, "volume": 10000},
        {"time": "2026-09-01 09:31:00", "close": 10.2, "volume": 10000},
        {"time": "2026-09-01 09:32:00", "close": 10.3, "volume": 10000},
    ])

    def signals(bar, _broker):
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        if ts == datetime(2026, 8, 31, 9, 31):
            return [StrategySignal("demo", "600000.SH", OrderSide.BUY, 500, ts, "建仓")]
        if ts == datetime(2026, 9, 1, 9, 31):
            return [StrategySignal("demo", "600000.SH", OrderSide.SELL, 200, ts, "减仓")]
        return []

    engine = OfflineReplayEngine(broker, store)
    engine.run(bars, "600000.SH", signals, autosave_every=2)
    assert engine.processed_bars == 4
    assert broker.account.positions["600000.SH"].quantity == 300
    assert len(broker.fills) >= 2
    assert sum(fill.quantity for fill in broker.fills if fill.side == OrderSide.BUY) == 500
    assert sum(fill.quantity for fill in broker.fills if fill.side == OrderSide.SELL) == 200
    assert store.snapshot_path.exists()
    assert store.audit_path.exists()

    restored = store.load()
    assert restored is not None
    assert restored.account.cash == broker.account.cash
    assert restored.account.positions["600000.SH"].quantity == 300
    assert len(restored.orders) == len(broker.orders)
    assert len(restored.fills) == len(broker.fills)

    first_order = next(order for order in restored.orders.values()
                       if order.request.side == OrderSide.BUY)
    assert first_order.status == OrderStatus.FILLED
    audit_lines = store.audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert audit_lines
    json.loads(audit_lines[-1])


def main():
    tests = [test_signal_id_is_deterministic, test_replay_persistence_and_recovery]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    result = OUTPUT_DIR.parent / "paper_replay_test_result.txt"
    result.write_text("P0下一阶段测试通过：信号适配、离线回放、持久化恢复共2项。\n",
                      encoding="utf-8")
    print(f"测试结果已保存：{result}")


if __name__ == "__main__":
    main()
