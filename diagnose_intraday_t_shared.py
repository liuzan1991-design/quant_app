# -*- coding: utf-8 -*-
"""定位日内做T共享状态机与黄金基准的首个分叉。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for path in (str(APP_DIR), str(PROJECT_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import backtest_generic as bg  # noqa: E402
from core.intraday_t_shared import IntradayTSharedEngine  # noqa: E402
from strategies.intraday_t import IntradayTStrategy  # noqa: E402

GOLDEN_DIR = Path(r"D:\Codex输出\日内做T黄金基准")


def run(code: str):
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    golden = pd.read_csv(GOLDEN_DIR / code / "trades.csv", parse_dates=["time"])
    golden_set = set(zip(golden["time"], golden["direction"]))

    strategy = IntradayTStrategy()
    params = strategy.default_params()
    params["stop_loss_pct"] = {
        "low": params.pop("stop_loss_low"),
        "normal": params.pop("stop_loss_normal"),
        "high": params.pop("stop_loss_high"),
    }
    params.setdefault("min_bars", 10)
    params.setdefault("buy_back_gap_pct", 0.005)
    params.setdefault("macd_fast", 5)
    params.setdefault("macd_slow", 10)
    params.setdefault("macd_signal", 3)
    params.setdefault("nine_turn_lookback", 3)

    engine = IntradayTSharedEngine(params)
    account = bg.SimAccount(1_000_000.0, params)
    window = int(params["min_bars"]) + 20
    closes_all = df["close"].to_numpy(float)
    highs_all = df["high"].to_numpy(float)
    lows_all = df["low"].to_numpy(float)
    volumes_all = df["volume"].to_numpy(float)
    bar_tail, rsi_all, vol_all, sig_high_all, sig_low_all = bg._precompute_indicators(
        closes_all, highs_all, lows_all, window,
        int(params["macd_fast"]), int(params["macd_slow"]), int(params["macd_signal"]),
        14, 14, int(params["nine_turn_lookback"]))

    day_amount = 0.0
    day_volume = 0.0
    prev_day = None
    for i, row in df.iterrows():
        ts = pd.Timestamp(row["time"])
        day = ts.date()
        if day != prev_day:
            day_amount = 0.0
            day_volume = 0.0
            prev_day = day
        day_amount += float(row["amount"])
        day_volume += float(row["volume"])
        vwap = day_amount / day_volume if day_volume else 0.0
        start = max(0, i - window + 1)
        indicator = {
            "closes": closes_all[start:i + 1],
            "volumes": volumes_all[start:i + 1],
            "macd_tail": bar_tail[i],
            "rsi": float(rsi_all[i]) if pd.notna(rsi_all[i]) else 50.0,
            "vol_level": ("low", "normal", "high")[int(vol_all[i])],
            "sig_high": bool(sig_high_all[i]),
            "sig_low": bool(sig_low_all[i]),
        }
        trade = engine.on_bar(row, account, indicator, vwap)
        if trade:
            key = (ts, trade["direction"])
            if key not in golden_set:
                print(f"首个仅共享信号：{code} {ts} {trade['direction']} {trade['shares']} {trade['reason']}")
                print("状态：", engine.state)
                print("账户：", account.__dict__)
                return
    print(f"{code} 未发现仅共享信号")


def main():
    for code in ("300308", "300502", "688256"):
        run(code)


if __name__ == "__main__":
    main()
