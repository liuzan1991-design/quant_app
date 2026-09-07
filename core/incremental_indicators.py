# -*- coding: utf-8 -*-
"""滚动窗口增量指标缓存。

用于替代 live 适配层里「一次性全量预计算 + 字典查表」的做法，使新 bar 到达时
也能现场算出自己的指标与当日累计 VWAP，不依赖未来数据、不 KeyError。

口径必须与 `backtest_generic._precompute_indicators` 逐位一致：
- 指标窗口为 min_bars + 20，只含当前 bar 及之前 bar；
- 成交量窗口同指标窗口；
- VWAP 用当日累计 amount/volume（真实价口径），含当前 bar。
"""
from __future__ import annotations

from typing import Tuple

import numpy as np
import pandas as pd

import backtest_generic as bg


class IncrementalIndicatorCache:
    def __init__(self, params: dict):
        self.params = dict(params)
        self.window = int(params.get("min_bars", 10)) + 20
        self._close = []
        self._high = []
        self._low = []
        self._volume = []
        self._day_amount = 0.0
        self._day_volume = 0.0
        self._prev_day = None

    def warmup(self, df: pd.DataFrame) -> None:
        """用历史数据预热滚动窗口，使缓存窗口长度与当日累计 VWAP 连续。

        只取最后 window 根 bar；但为让跨日 VWAP 口径正确，需保留「当日累计」的
        起点——因此从最后 window 根里，若跨日则只取最后一天之前的完整预热，
        实际实现按最后 window 根逐根 push（push 内部会按 day 重置当日累计）。
        """
        if df is None or len(df) == 0:
            return
        df = df.reset_index(drop=True)
        # 指标窗口只需最后 window 根，但 VWAP 的「当日累计」必须从最后交易日开盘
        # 连续累积，否则新 bar 的 VWAP 起点错位。因此从最后一个完整交易日的
        # 第一根 bar 开始 push，既补满窗口（若足够），又让当日累计正确。
        last_day = pd.Timestamp(df["time"].iloc[-1]).date()
        day_mask = pd.to_datetime(df["time"]).dt.date == last_day
        day_rows = df[day_mask]
        # 保底：若最后一天不足 window（罕见），也带上前一天尾部补窗口
        if len(day_rows) < self.window:
            tail = df.tail(self.window)
        else:
            tail = day_rows
        for _, row in tail.iterrows():
            self.push(
                row["time"],
                float(row.get("signal_close", row.get("close"))),
                float(row.get("signal_high", row.get("high"))),
                float(row.get("signal_low", row.get("low"))),
                float(row.get("volume", 0) or 0),
                float(row.get("amount", 0) or 0),
                float(row.get("volume", 0) or 0),
            )

    def push(self, ts, signal_close: float, signal_high: float, signal_low: float,
             volume: float, raw_amount: float, raw_volume: float) -> Tuple[dict, float]:
        """压入当前 bar，返回 (indicator_dict, vwap)。"""
        day = pd.Timestamp(ts).date()
        if day != self._prev_day:
            self._day_amount = 0.0
            self._day_volume = 0.0
            self._prev_day = day

        self._close.append(float(signal_close))
        self._high.append(float(signal_high))
        self._low.append(float(signal_low))
        self._volume.append(float(volume))
        if len(self._close) > self.window:
            self._close.pop(0)
            self._high.pop(0)
            self._low.pop(0)
            self._volume.pop(0)

        self._day_amount += float(raw_amount or 0)
        self._day_volume += float(raw_volume or 0)
        vwap = self._day_amount / self._day_volume if self._day_volume else 0.0

        closes = np.asarray(self._close, dtype=np.float64)
        highs = np.asarray(self._high, dtype=np.float64)
        lows = np.asarray(self._low, dtype=np.float64)
        volumes = np.asarray(self._volume, dtype=np.float64)
        p = self.params
        bar_tail, rsi_all, vol_all, sig_high_all, sig_low_all = bg._precompute_indicators(
            closes, highs, lows, self.window,
            int(p.get("macd_fast", 5)), int(p.get("macd_slow", 10)),
            int(p.get("macd_signal", 3)), 14, 14, int(p.get("nine_turn_lookback", 3)))

        indicator = {
            "closes": closes,
            "volumes": volumes,
            "macd_tail": bar_tail[-1],
            "rsi": float(rsi_all[-1]) if pd.notna(rsi_all[-1]) else 50.0,
            "vol_level": ("low", "normal", "high")[int(vol_all[-1])],
            "sig_high": bool(sig_high_all[-1]),
            "sig_low": bool(sig_low_all[-1]),
        }
        return indicator, vwap