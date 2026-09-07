# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.adjustment import prepare_signal_prices
from core.ma_swing_live import MaSwingLiveEngine
from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_replay import OfflineReplayEngine
from core.risk_manager import RiskConfig, RiskManager
from strategies.ma_swing import MaSwingStrategy

OUT = APP_DIR / "test_outputs" / "ma_swing_live"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for code in ["300308", "300502", "688256"]:
        df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
        symbol = f"{code}.SH" if code.startswith("68") else f"{code}.SZ"
        params = MaSwingStrategy.default_params()
        prepared, corporate_dates, _ = prepare_signal_prices(df, code)
        live = MaSwingLiveEngine(symbol, params, prepared_df=prepared,
                                 corporate_action_dates=corporate_dates)
        risk = RiskManager(RiskConfig(max_symbol_position_pct=1, max_account_position_pct=1,
                                      max_order_value_pct=1, max_daily_loss_pct=1,
                                      max_symbol_cumulative_loss_pct=1))
        broker = PaperBroker(1_000_000, df.time.iloc[0].date().isoformat(), risk,
                             PaperBrokerConfig(slippage_bps=5, max_volume_participation=1))
        OfflineReplayEngine(broker).run(df, symbol, live.on_bar)
        broker.account.positions.get(symbol) and setattr(
            broker.account.positions[symbol], "market_price", float(df.close.iloc[-1]))
        fast = MaSwingStrategy().run(df, 1_000_000, params, context={"code": code})
        rows.append({
            "股票": code,
            "严格增量订单数": len(broker.orders),
            "严格增量成交数": len(broker.fills),
            "严格增量收益率": broker.account.equity() / 1_000_000 - 1,
            "原快速回测交易数": len(fast.trades),
            "原快速回测收益率": fast.total_return,
            "数据开始": df.time.min(),
            "数据结束": df.time.max(),
        })
    result = pd.DataFrame(rows)
    result.to_csv(OUT / "ma_swing_live_candidates.csv", index=False, encoding="utf-8-sig")
    print(result.to_string(index=False))


if __name__ == "__main__":
    main()
