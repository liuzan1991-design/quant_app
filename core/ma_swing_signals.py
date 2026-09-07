# -*- coding: utf-8 -*-
"""均线波段公共日线信号核心：快速回测与严格增量路径共用。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def build_daily_signal_table(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    bars = df.copy()
    bars["date"] = pd.to_datetime(bars["time"]).dt.normalize()
    for col in ("signal_high", "signal_low", "signal_close"):
        if col not in bars.columns:
            bars[col] = bars[col.replace("signal_", "")]
    daily = bars.groupby("date", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), signal_high=("signal_high", "max"),
        signal_low=("signal_low", "min"), signal_close=("signal_close", "last"),
        volume=("volume", "sum"),
    )
    close, high, low = daily["signal_close"], daily["signal_high"], daily["signal_low"]
    for key in ("fast_ma", "pullback_ma", "trend_ma", "slow_ma"):
        period = int(params[key])
        daily[key] = close.rolling(period, min_periods=period).mean()
    delta = close.diff()
    n_rsi = int(params["rsi_period"])
    gain = delta.clip(lower=0).ewm(alpha=1 / n_rsi, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n_rsi, adjust=False).mean()
    daily["rsi"] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).fillna(100.0)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    daily["atr"] = tr.ewm(alpha=1 / int(params["atr_period"]), adjust=False).mean()
    tol = float(params["pullback_tolerance_pct"]) / 100.0
    trend = ((close > daily["trend_ma"]) &
             (daily["trend_ma"] > daily["slow_ma"]) &
             (daily["trend_ma"] > daily["trend_ma"].shift(5)))
    if bool(params["strict_alignment"]):
        trend &= ((daily["fast_ma"] > daily["pullback_ma"]) &
                  (daily["pullback_ma"] > daily["trend_ma"]))
    touch = low <= daily["pullback_ma"] * (1 + tol)
    recovered = close >= daily["pullback_ma"] * (1 - tol)
    daily["pullback_entry"] = touch & recovered & (daily["rsi"] <= float(params["rsi_max"]))
    n = int(params["breakout_days"])
    prev_high = high.rolling(n, min_periods=n).max().shift(1)
    avg_volume = daily["volume"].rolling(n, min_periods=n).mean().shift(1)
    daily["breakout_entry"] = ((close > prev_high) &
                               (daily["volume"] >= avg_volume * float(params["breakout_volume_multiple"])))
    daily["trend_ok"] = trend
    daily["entry_type"] = np.where(trend & daily["breakout_entry"], "趋势放量突破",
                                    np.where(trend & daily["pullback_entry"], "多头趋势回踩均线", ""))
    daily["buy_signal"] = daily["entry_type"] != ""
    below = close < daily["trend_ma"]
    exit_days = int(params["trend_exit_days"])
    daily["trend_exit"] = below.rolling(exit_days, min_periods=exit_days).sum() >= exit_days
    return daily


def shifted_execution_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    cols = ["buy_signal", "entry_type", "trend_exit", "atr", "trend_ma",
            "trend_ok", "pullback_entry", "breakout_entry", "rsi"]
    return build_daily_signal_table(df, params)[cols].shift(1)


def calculate_order_shares(cash: float, price: float, params: dict) -> int:
    fee_buffer = (1 + float(params.get("slippage_bps", 5)) / 10000.0
                  + float(params.get("commission_rate", 0.0001))
                  + float(params.get("transfer_fee_rate", 0.00002)))
    budget = cash * float(params.get("position_pct", 90.0)) / 100.0
    return max(0, int(budget / (price * fee_buffer) // 100 * 100))
