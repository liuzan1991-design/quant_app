# -*- coding: utf-8 -*-
"""日内做T共享状态机的离线回放适配层。

复用 `IntradayTSharedEngine` 的信号与状态逻辑，把每根 bar 上的交易意图
转成 `StrategySignal`，供 `OfflineReplayEngine` 交给 `PaperBroker` 撮合。
信号生成与 `test_intraday_t_shared_consistency.py` 保持同一口径：
策略状态用 `bg.SimAccount` 驱动，PaperBroker 只负责订单撮合与审计。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Iterable, Optional

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = Path(__file__).resolve().parents[2]
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np
import pandas as pd

import backtest_generic as bg
from core.adjustment import prepare_signal_prices
from core.incremental_indicators import IncrementalIndicatorCache
from core.intraday_t_shared import IntradayTSharedEngine
from core.paper_models import OrderSide
from core.paper_replay import StrategySignal


class IntradayTSharedLiveEngine:
    def __init__(self, symbol: str, params: Dict,
                 strategy_id: str = "intraday_t",
                 df: Optional[pd.DataFrame] = None):
        self.symbol = symbol
        self.strategy_id = strategy_id
        self.params = dict(params)
        self.engine = IntradayTSharedEngine(self.params)
        self.account = bg.SimAccount(1_000_000.0, self.params)
        self.window = int(self.params.get("min_bars", 10)) + 20
        self._bar_index: Dict[pd.Timestamp, int] = {}
        self._vwap: Dict[pd.Timestamp, float] = {}
        self._indicator: Dict[pd.Timestamp, dict] = {}
        self._prepared = False
        self._code = symbol.split(".")[0]
        self._inc = IncrementalIndicatorCache(self.params)
        if df is not None:
            self.prepare(df)

    def prepare(self, df: pd.DataFrame) -> None:
        if self._prepared:
            return
        prepared, _events, _adj = prepare_signal_prices(df, self._code)
        closes_all = prepared["signal_close"].to_numpy(float)
        highs_all = prepared["signal_high"].to_numpy(float)
        lows_all = prepared["signal_low"].to_numpy(float)
        volumes_all = prepared["volume"].to_numpy(float)
        p = self.params
        bar_tail, rsi_all, vol_all, sig_high_all, sig_low_all = bg._precompute_indicators(
            closes_all, highs_all, lows_all, self.window,
            int(p.get("macd_fast", 5)), int(p.get("macd_slow", 10)),
            int(p.get("macd_signal", 3)), 14, 14, int(p.get("nine_turn_lookback", 3)))

        day_amount = 0.0
        day_volume = 0.0
        prev_day = None
        for i, row in prepared.iterrows():
            ts = pd.Timestamp(row["time"])
            day = ts.date()
            if day != prev_day:
                day_amount = 0.0
                day_volume = 0.0
                prev_day = day
            day_amount += float(row["amount"])
            day_volume += float(row["volume"])
            self._bar_index[ts] = i
            self._vwap[ts] = day_amount / day_volume if day_volume else 0.0
            start = max(0, i - self.window + 1)
            self._indicator[ts] = {
                "closes": closes_all[start:i + 1],
                "volumes": volumes_all[start:i + 1],
                "macd_tail": bar_tail[i],
                "rsi": float(rsi_all[i]) if pd.notna(rsi_all[i]) else 50.0,
                "vol_level": ("low", "normal", "high")[int(vol_all[i])],
                "sig_high": bool(sig_high_all[i]),
                "sig_low": bool(sig_low_all[i]),
            }
        # 状态推进：先建独立缓存回放完整历史，推进引擎与账户状态；
        # 再单独给实时指标缓存 warmup 最后一天，避免两者互相污染。
        replay_cache = IncrementalIndicatorCache(self.params)
        self.engine.replay_warmup(prepared, self.account, replay_cache)
        self._prepared = True

    def on_bar(self, bar: pd.Series, _broker) -> Iterable[StrategySignal]:
        ts = pd.Timestamp(bar["time"])
        if not self._prepared:
            raise RuntimeError("IntradayTSharedLiveEngine.prepare(df) 未调用")
        if ts in self._indicator:
            indicator = self._indicator[ts]
            vwap = self._vwap[ts]
        else:
            indicator, vwap = self._inc.push(
                ts,
                float(bar.get("signal_close", bar.get("close"))),
                float(bar.get("signal_high", bar.get("high"))),
                float(bar.get("signal_low", bar.get("low"))),
                float(bar.get("volume", 0) or 0),
                float(bar.get("amount", 0) or 0),
                float(bar.get("volume", 0) or 0),
            )
        trade = self.engine.on_bar(bar, self.account, indicator, vwap)
        if not trade:
            return ()
        side = OrderSide.BUY if trade["direction"] == "BUY" else OrderSide.SELL
        return (StrategySignal(
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            side=side,
            quantity=int(trade["shares"]),
            signal_time=ts.to_pydatetime(),
            reason=trade.get("reason", ""),
        ),)