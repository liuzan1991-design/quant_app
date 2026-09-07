# -*- coding: utf-8 -*-
"""大盘情绪做T严格增量引擎。

复用已通过 95% 门禁的 `IntradayTSharedEngine`，仅把大盘状态、指数涨跌、
个股趋势模式作为逐bar上下文注入。信号生成逻辑与快速路径 `run_backtest`
一致；情绪/趋势数据在启动前一次性预计算，回放阶段只做查找，不产生未来数据。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backtest_generic as bg  # noqa: E402
from core import sentiment_data  # noqa: E402
from core.adjustment import prepare_signal_prices  # noqa: E402
from core.incremental_indicators import IncrementalIndicatorCache  # noqa: E402
from core.intraday_t_shared import IntradayTSharedEngine  # noqa: E402
from core.paper_models import OrderSide  # noqa: E402
from core.paper_replay import StrategySignal  # noqa: E402


class SentimentTSharedLiveEngine:
    def __init__(self, symbol: str, params: Dict, df: Optional[pd.DataFrame] = None,
                 strategy_id: str = "sentiment_t"):
        self.symbol = symbol
        self.strategy_id = strategy_id
        self.params = dict(params)
        self.params.setdefault("min_bars", 10)
        self.params.setdefault("macd_fast", 5)
        self.params.setdefault("macd_slow", 10)
        self.params.setdefault("macd_signal", 3)
        self.params.setdefault("nine_turn_lookback", 3)
        self.params.setdefault("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
        self.params.setdefault("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
        self.params.setdefault("buy_back_gap_pct", 0.005)
        self.engine = IntradayTSharedEngine(self.params)
        self.account = bg.SimAccount(1_000_000.0, self.params)
        self._status_map: Dict[pd.Timestamp, str] = {}
        self._chg_map: Dict[pd.Timestamp, float] = {}
        self._mode_map: Dict[object, str] = {}
        self._indicator: Dict[pd.Timestamp, dict] = {}
        self._vwap: Dict[pd.Timestamp, float] = {}
        self._prepared = False
        self._code = symbol.split(".")[0]
        self._inc = IncrementalIndicatorCache(self.params)
        if df is not None:
            self.prepare(df)

    def prepare(self, df: pd.DataFrame) -> None:
        if self._prepared:
            return
        p = self.params
        prepared, _events, _adj = prepare_signal_prices(df, self._code)
        start = pd.Timestamp(prepared["time"].iloc[0]).strftime("%Y-%m-%d")
        end = pd.Timestamp(prepared["time"].iloc[-1]).strftime("%Y-%m-%d")
        index_code = p.get("index_code", "399006.SZ")
        try:
            idx = sentiment_data.load_index_min(index_code, start, end)
            status_map, chg_map = sentiment_data.compute_market_status(idx)
        except Exception:
            status_map, chg_map = {}, {}

        if p.get("use_sector", True) and self._code:
            try:
                ind_code = sentiment_data.get_stock_industry_index(self._code)
                if ind_code:
                    ind_daily = sentiment_data.load_industry_daily(ind_code, start, end)
                    sector = sentiment_data.compute_sector_status(ind_daily)
                    if sector == "weak" and status_map:
                        status_map = {ts: ("weak" if st != "crash" else st)
                                      for ts, st in status_map.items()}
                    elif sector == "strong" and status_map:
                        status_map = {ts: ("strong" if st in ("flat", "strong") else st)
                                      for ts, st in status_map.items()}
            except Exception:
                pass

        mode_map = {}
        if p.get("trend_mode", True):
            try:
                trend_map = sentiment_data.compute_trend_mode(
                    prepared, ma_trend=int(p.get("trend_ma", 20)))
                mode_map = trend_map
                if status_map:
                    for ts in list(status_map.keys()):
                        day = pd.Timestamp(ts).date()
                        if trend_map.get(day) == "defense" and status_map[ts] != "crash":
                            status_map[ts] = "weak"
            except Exception:
                pass

        self._status_map = status_map
        self._chg_map = chg_map
        self._mode_map = mode_map

        closes_all = prepared["signal_close"].to_numpy(float)
        highs_all = prepared["signal_high"].to_numpy(float)
        lows_all = prepared["signal_low"].to_numpy(float)
        volumes_all = prepared["volume"].to_numpy(float)
        window = int(self.params.get("min_bars", 10)) + 20
        bar_tail, rsi_all, vol_all, sig_high_all, sig_low_all = bg._precompute_indicators(
            closes_all, highs_all, lows_all, window,
            int(self.params.get("macd_fast", 5)), int(self.params.get("macd_slow", 10)),
            int(self.params.get("macd_signal", 3)), 14, 14, int(self.params.get("nine_turn_lookback", 3)))
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
            start = max(0, i - window + 1)
            self._indicator[ts] = {
                "closes": closes_all[start:i + 1],
                "volumes": volumes_all[start:i + 1],
                "macd_tail": bar_tail[i],
                "rsi": float(rsi_all[i]) if pd.notna(rsi_all[i]) else 50.0,
                "vol_level": ("low", "normal", "high")[int(vol_all[i])],
                "sig_high": bool(sig_high_all[i]),
                "sig_low": bool(sig_low_all[i]),
            }
            self._vwap[ts] = day_amount / day_volume if day_volume else 0.0
        # 状态推进：先建独立缓存回放完整历史，推进引擎与账户状态；
        # 再单独给实时指标缓存 warmup 最后一天，避免两者互相污染。
        replay_cache = IncrementalIndicatorCache(self.params)
        self.engine.replay_warmup(prepared, self.account, replay_cache)
        self._prepared = True

    def on_bar(self, bar: pd.Series, _broker) -> Iterable[StrategySignal]:
        ts = pd.Timestamp(bar["time"])
        if not self._prepared:
            raise RuntimeError("SentimentTSharedLiveEngine.prepare(df) 未调用")
        day = ts.date()
        self.engine.set_context(
            index_status=self._status_map.get(ts, "flat"),
            index_change_pct=self._chg_map.get(ts, 0.0),
            day_mode=self._mode_map.get(day, "range"))
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