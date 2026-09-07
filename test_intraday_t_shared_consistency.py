# -*- coding: utf-8 -*-
"""共享状态机 vs 黄金基准一致性验证。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
PROJECT_DIR = APP_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import backtest_generic as bg  # noqa: E402
from core.intraday_t_shared import IntradayTSharedEngine  # noqa: E402
from strategies.intraday_t import IntradayTStrategy  # noqa: E402

GOLDEN_DIR = Path(r"D:\Codex输出\日内做T黄金基准")


def run_one(code: str) -> dict:
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    strategy = IntradayTStrategy()
    params = strategy.default_params()
    params["stop_loss_pct"] = {
        "low": params.pop("stop_loss_low"),
        "normal": params.pop("stop_loss_normal"),
        "high": params.pop("stop_loss_high"),
    }
    params.setdefault("min_bars", 10)
    params.setdefault("macd_fast", 5)
    params.setdefault("macd_slow", 10)
    params.setdefault("macd_signal", 3)
    params.setdefault("nine_turn_lookback", 3)
    params.setdefault("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
    params.setdefault("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
    params.setdefault("max_position", 6000)
    params.setdefault("max_buys_per_day", 10)
    params.setdefault("max_sells_per_day", 10)
    params.setdefault("max_daily_stop_loss", 1)
    params.setdefault("buy_back_gap_pct", 0.005)
    engine = IntradayTSharedEngine(params)
    account = bg.SimAccount(1_000_000.0, params)
    trades = []
    equity_curve = []

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
            trade["time"] = ts
            trade["score"] = ""
            trade["t_pnl"] = None
            trades.append(trade)
        equity_curve.append({
            "time": ts,
            "equity": account.equity(float(row["close"])),
            "close": float(row["close"]),
            "cash": account.cash,
            "position": account.total,
        })

    result = pd.DataFrame(trades)
    output_dir = Path(r"D:\Codex输出\日内做T共享状态机")
    output_dir.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_dir / f"{code}_shared_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(equity_curve).to_csv(
        output_dir / f"{code}_shared_equity.csv", index=False, encoding="utf-8-sig")
    golden = pd.read_csv(GOLDEN_DIR / code / "trades.csv", parse_dates=["time"])
    merged = result.merge(golden, on=["time", "direction"], how="outer",
                          suffixes=("_shared", "_golden"), indicator=True)
    matched = merged[merged["_merge"] == "both"]
    rate = len(matched) / max(len(result), len(golden), 1)
    return {
        "code": code,
        "shared_count": len(result),
        "golden_count": len(golden),
        "matched": len(matched),
        "match_rate": rate,
    }


def main() -> None:
    rows = [run_one(code) for code in ("300308", "300502", "688256")]
    print(pd.DataFrame(rows).to_string(index=False))
    if any(row["match_rate"] < 0.95 for row in rows):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
