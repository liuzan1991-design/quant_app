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

OUT = APP_DIR / "test_outputs" / "ma_swing_reconcile"


def run_live(df, code, params):
    symbol = f"{code}.SH" if code.startswith("68") else f"{code}.SZ"
    risk = RiskManager(RiskConfig(max_symbol_position_pct=1, max_account_position_pct=1,
                                  max_order_value_pct=1, max_daily_loss_pct=1,
                                  max_symbol_cumulative_loss_pct=1))
    broker = PaperBroker(1_000_000, df.time.iloc[0].date().isoformat(), risk,
                         PaperBrokerConfig(
                             commission_rate=float(params.get("commission_rate", 0.0001)),
                             min_commission=float(params.get("min_commission", 0.0)),
                             stamp_tax_rate=float(params.get("stamp_tax_rate", 0.001)),
                             transfer_fee_rate=float(params.get("transfer_fee_rate", 0.00002)),
                             slippage_bps=float(params.get("slippage_bps", 5)),
                             max_volume_participation=1,
                         ))
    prepared, corporate_dates, _ = prepare_signal_prices(df, code)
    engine = MaSwingLiveEngine(symbol, params, prepared_df=prepared,
                               corporate_action_dates=corporate_dates)
    OfflineReplayEngine(broker).run(df, symbol, engine.on_bar)
    rows = []
    for order in broker.orders.values():
        rows.append({"time": order.request.created_at, "direction": order.request.side.value,
                     "shares": order.request.quantity, "price": order.average_fill_price,
                     "reason": order.request.reason if hasattr(order.request, "reason") else "严格增量"})
    return pd.DataFrame(rows), broker


def reconcile(code):
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    params = MaSwingStrategy.default_params()
    fast = MaSwingStrategy().run(df, 1_000_000, params, context={"code": code}).trades.copy()
    live, broker = run_live(df, code, params)
    fast["day"] = pd.to_datetime(fast["time"]).dt.date
    live["day"] = pd.to_datetime(live["time"]).dt.date
    fast["seq"] = fast.groupby(["day", "direction"]).cumcount()
    live["seq"] = live.groupby(["day", "direction"]).cumcount()
    merged = fast.merge(live, on=["day", "direction", "seq"], how="outer",
                        suffixes=("_fast", "_live"), indicator=True)
    merged["time_diff_minutes"] = (
        pd.to_datetime(merged["time_live"]) - pd.to_datetime(merged["time_fast"])
    ).dt.total_seconds() / 60
    merged["share_diff"] = merged.get("shares_live", 0) - merged.get("shares_fast", 0)
    merged["price_diff_pct"] = merged.get("price_live", 0) / merged.get("price_fast", 1) - 1
    merged.insert(0, "股票", code)
    return merged, fast, live, broker


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    all_rows, summary = [], []
    for code in ["300308", "300502", "688256"]:
        merged, fast, live, broker = reconcile(code)
        merged.to_csv(OUT / f"{code}_trade_reconcile.csv", index=False, encoding="utf-8-sig")
        all_rows.append(merged)
        summary.append({
            "股票": code, "快速交易数": len(fast), "增量交易数": len(live),
            "同日同向匹配": int((merged["_merge"] == "both").sum()),
            "仅快速": int((merged["_merge"] == "left_only").sum()),
            "仅增量": int((merged["_merge"] == "right_only").sum()),
            "平均时间差分钟": merged.loc[merged["_merge"] == "both", "time_diff_minutes"].mean(),
            "平均数量差": merged.loc[merged["_merge"] == "both", "share_diff"].mean(),
        })
    pd.concat(all_rows, ignore_index=True).to_csv(
        OUT / "all_trade_reconcile.csv", index=False, encoding="utf-8-sig")
    summary_df = pd.DataFrame(summary)
    summary_df.to_csv(OUT / "reconcile_summary.csv", index=False, encoding="utf-8-sig")
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()
