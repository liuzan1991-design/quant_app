# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.intraday_t_shared_live import IntradayTSharedLiveEngine
from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_replay import OfflineReplayEngine
from core.risk_manager import RiskConfig, RiskManager
from strategies.intraday_t import IntradayTStrategy

OUT = APP_DIR / "test_outputs" / "intraday_t_dual_gate"
SIGNAL_GATE = 0.95
FILL_GATE = 0.90


def run(code):
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    symbol = f"{code}.SH" if code.startswith("68") else f"{code}.SZ"
    strategy = IntradayTStrategy()
    params = strategy.default_params()
    engine_params = dict(params)
    engine_params.setdefault("min_bars", 10)
    engine_params.setdefault("macd_fast", 5)
    engine_params.setdefault("macd_slow", 10)
    engine_params.setdefault("macd_signal", 3)
    engine_params.setdefault("nine_turn_lookback", 3)
    engine_params.setdefault("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
    engine_params.setdefault("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
    engine_params.setdefault("max_position", 6000)
    engine_params.setdefault("max_buys_per_day", 10)
    engine_params.setdefault("max_sells_per_day", 10)
    engine_params.setdefault("max_daily_stop_loss", 1)
    engine_params.setdefault("buy_back_gap_pct", 0.005)
    engine_params["stop_loss_pct"] = {
        "low": engine_params.pop("stop_loss_low"),
        "normal": engine_params.pop("stop_loss_normal"),
        "high": engine_params.pop("stop_loss_high"),
    }
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
    live = IntradayTSharedLiveEngine(symbol, engine_params, df=df)
    replay = OfflineReplayEngine(broker)
    replay.run(df, symbol, live.on_bar)

    fast_signals = fast.trades[["time", "direction", "shares", "reason"]].copy()
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
        raise AssertionError("日内做T双门禁未通过：" + ", ".join(failures))


if __name__ == "__main__":
    main()
