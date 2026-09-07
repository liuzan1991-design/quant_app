# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_replay import OfflineReplayEngine
from core.risk_manager import RiskConfig, RiskManager
from core.strategy_signal_adapter import HistoricalStrategySignalAdapter

DATA_DIR = APP_DIR / "data"
OUTPUT_DIR = APP_DIR / "test_outputs" / "candidate_replay"
CANDIDATES = ["300308", "300502", "688256"]
STRATEGIES = ["intraday_t", "grid_trade", "sentiment_t", "ma_swing"]


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for code in CANDIDATES:
        path = DATA_DIR / f"stock_{code}_1m.csv"
        df = pd.read_csv(path, parse_dates=["time"])
        symbol = f"{code}.SH" if code.startswith(("60", "68")) else f"{code}.SZ"
        for strategy_id in STRATEGIES:
            try:
                prepared = HistoricalStrategySignalAdapter.prepare(
                    strategy_id, df, symbol, 1_000_000.0)
                risk = RiskManager(RiskConfig(
                    max_symbol_position_pct=1.00,
                    max_account_position_pct=1.00,
                    max_order_value_pct=1.00,
                    max_daily_loss_pct=1.00,
                    max_symbol_cumulative_loss_pct=1.00,
                ))
                broker = PaperBroker(
                    1_000_000.0,
                    pd.Timestamp(df["time"].iloc[0]).date().isoformat(),
                    risk_manager=risk,
                    config=PaperBrokerConfig(slippage_bps=5, max_volume_participation=0.10),
                )
                engine = OfflineReplayEngine(broker)
                engine.run(df, symbol, prepared.on_bar)
                final_price = float(df["close"].iloc[-1])
                broker.mark_to_market(type("Snapshot", (), {"symbol": symbol, "last_price": final_price})())
                rows.append({
                    "股票": code,
                    "策略": strategy_id,
                    "快速回测收益率": prepared.backtest_result.total_return,
                    "事件回放收益率": broker.account.equity() / 1_000_000.0 - 1,
                    "适配信号数": sum(len(v) for v in prepared.signals_by_time.values()),
                    "模拟订单数": len(broker.orders),
                    "成交记录数": len(broker.fills),
                    "拒单数": sum(1 for order in broker.orders.values() if order.status.value == "REJECTED"),
                    "状态": "通过",
                })
            except Exception as exc:
                rows.append({"股票": code, "策略": strategy_id, "状态": f"失败：{exc}"})
    result = pd.DataFrame(rows)
    result.to_csv(OUTPUT_DIR / "candidate_replay_summary.csv", index=False, encoding="utf-8-sig")
    (OUTPUT_DIR / "candidate_replay_summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(result.to_string(index=False))
    print(f"\n结果已保存：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
