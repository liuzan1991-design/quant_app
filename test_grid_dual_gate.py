# -*- coding: utf-8 -*-
"""移动网格严格增量双门禁：快速路径 vs 共享状态机+PaperBroker。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.grid_shared_live import GridSharedLiveEngine  # noqa: E402
from core.paper_broker import PaperBroker, PaperBrokerConfig  # noqa: E402
from core.paper_replay import OfflineReplayEngine  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager  # noqa: E402
from strategies.grid_trade import GridTradeStrategy  # noqa: E402

OUT = APP_DIR / "test_outputs" / "grid_dual_gate"
SIGNAL_GATE = 0.95
FILL_GATE = 0.90


def run(code):
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    symbol = f"{code}.SH" if code.startswith("68") else f"{code}.SZ"
    strategy = GridTradeStrategy()
    params = strategy.default_params()
    fast = strategy.run(df, 1_000_000, params, context={"code": code})

    risk = RiskManager(RiskConfig(max_symbol_position_pct=1, max_account_position_pct=1,
                                  max_order_value_pct=1, max_daily_loss_pct=1,
                                  max_symbol_cumulative_loss_pct=1))
    broker = PaperBroker(1_000_000, df.time.iloc[0].date().isoformat(), risk,
                         PaperBrokerConfig(
                             commission_rate=params["commission_rate"],
                             min_commission=params["min_commission"],
                             stamp_tax_rate=params["stamp_tax_rate"],
                             transfer_fee_rate=params["transfer_fee_rate"],
                             slippage_bps=params["slippage_bps"],
                             max_volume_participation=1))
    live = GridSharedLiveEngine(symbol, params, df=df)
    replay = OfflineReplayEngine(broker)
    replay.run(df, symbol, live.on_bar)

    fast_signals = fast.trades[["time", "direction", "shares"]].copy()
    fast_signals["day"] = pd.to_datetime(fast_signals["time"]).dt.date
    live_signals = pd.DataFrame(replay.signal_log)
    live_signals["day"] = pd.to_datetime(live_signals["signal_time"]).dt.date
    live_signals = live_signals.rename(columns={"side": "direction"})
    fast_signals["seq"] = fast_signals.groupby(["day", "direction"]).cumcount()
    live_signals["seq"] = live_signals.groupby(["day", "direction"]).cumcount()
    compare = fast_signals.merge(live_signals, on=["day", "direction", "seq"],
                                 how="outer", suffixes=("_fast", "_live"), indicator=True)
    matched = int((compare["_merge"] == "both").sum())
    denominator = max(len(fast_signals), len(live_signals), 1)
    signal_rate = matched / denominator
    accepted = live_signals[live_signals["order_status"] != "REJECTED"]
    filled = accepted[accepted["filled_quantity"] > 0]
    fill_rate = len(filled) / max(len(accepted), 1)

    OUT.mkdir(parents=True, exist_ok=True)
    compare.to_csv(OUT / f"{code}_signal_compare.csv", index=False, encoding="utf-8-sig")
    live_signals.to_csv(OUT / f"{code}_raw_signals.csv", index=False, encoding="utf-8-sig")
    return {"股票": code, "快速信号": len(fast_signals), "增量信号": len(live_signals),
            "匹配": matched, "信号匹配率": signal_rate,
            "信号门禁": signal_rate >= SIGNAL_GATE,
            "接受订单": len(accepted), "成交订单": len(filled),
            "成交率": fill_rate, "成交门禁": fill_rate >= FILL_GATE}


def main():
    rows = [run(code) for code in ("300308", "300502", "688256")]
    result = pd.DataFrame(rows)
    result.to_csv(OUT / "dual_gate_summary.csv", index=False, encoding="utf-8-sig")
    print(result.to_string(index=False))
    failures = []
    for row in rows:
        if not row["信号门禁"]:
            failures.append(f'{row["股票"]}信号={row["信号匹配率"]:.2%}')
        if not row["成交门禁"]:
            failures.append(f'{row["股票"]}成交={row["成交率"]:.2%}')
    if failures:
        raise AssertionError("网格双门禁未通过：" + ", ".join(failures))


if __name__ == "__main__":
    main()