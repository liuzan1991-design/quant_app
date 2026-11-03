# -*- coding: utf-8 -*-
"""大盘情绪做T严格增量引擎。

复用已通过 95% 门禁的 `IntradayTSharedEngine`，仅把大盘状态、指数涨跌、
个股趋势模式作为逐bar上下文注入。生产模式按已完成的日线日期取上下文；
历史参考模式保留旧逻辑，用于与快速路径核对。
"""
from __future__ import annotations

import sys
from bisect import bisect_left, insort
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
from core.intraday_t_broker import BrokerDrivenTMixin  # noqa: E402
from core.paper_models import OrderSide  # noqa: E402
from core.paper_replay import StrategySignal  # noqa: E402


class SentimentTSharedLiveEngine(BrokerDrivenTMixin):
    def __init__(self, symbol: str, params: Dict, df: Optional[pd.DataFrame] = None,
                 strategy_id: str = "sentiment_t", *, broker_driven: bool = False):
        self.symbol = symbol
        self.strategy_id = strategy_id
        self.params = dict(params)
        if broker_driven:
            from core.strategy_params import normalize_t_params
            self.params = normalize_t_params(self.params)
        self.params.setdefault("min_bars", 10)
        self.params.setdefault("macd_fast", 5)
        self.params.setdefault("macd_slow", 10)
        self.params.setdefault("macd_signal", 3)
        self.params.setdefault("nine_turn_lookback", 3)
        self.params.setdefault("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
        self.params.setdefault("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
        self.params.setdefault("buy_back_gap_pct", 0.005)
        self.engine = IntradayTSharedEngine(self.params)
        self.broker_driven = broker_driven
        self.account = None if broker_driven else bg.SimAccount(1_000_000.0, self.params)
        self._status_map: Dict[pd.Timestamp, str] = {}
        self._chg_map: Dict[pd.Timestamp, float] = {}
        self._mode_map: Dict[object, str] = {}
        self._sector_closes: dict = {}
        self._sector_days: list = []
        self._stock_closes: dict = {}
        self._session_days: list = []
        self._indicator: Dict[pd.Timestamp, dict] = {}
        self._vwap: Dict[pd.Timestamp, float] = {}
        self._prepared = False
        self._code = symbol.split(".")[0]
        self._inc = IncrementalIndicatorCache(self.params)
        if df is not None:
            self.prepare(df)

    @staticmethod
    def _is_session_close(ts):
        # 项目分钟线通常用14:59标记最后一分钟，兼容15:00的结束时刻标签。
        return (ts.hour, ts.minute) in {(14, 59), (15, 0)}

    def _update_trend_at_closes(self):
        if self._stock_closes and self.params.get("trend_mode", True):
            daily = pd.DataFrame({"time": pd.to_datetime(list(self._stock_closes)),
                                  "signal_close": list(self._stock_closes.values())})
            self._mode_map = sentiment_data.compute_trend_mode(
                daily, ma_trend=int(self.params.get("trend_ma", 20)))

    def _prepare_causal_context(self, prepared, start, end):
        self._session_days = sorted(set(prepared["time"].dt.date))
        if not self.params.get("enable_sentiment_filter", True):
            return
        index_code = self.params.get("index_code", "399006.SZ")
        try:
            index = sentiment_data.load_index_min(index_code, start, end)
            index = index.sort_values("time").drop_duplicates("time", keep="last")
            self._status_map, self._chg_map = sentiment_data.compute_market_status(index)
        except Exception:
            pass

        if self.params.get("use_sector", True) and self._code:
            try:
                industry = sentiment_data.get_stock_industry_index(self._code)
                if industry:
                    warmup = (pd.Timestamp(start) - pd.Timedelta(days=60)).date().isoformat()
                    daily = sentiment_data.load_industry_daily(industry, warmup, end)
                    self._sector_closes = sentiment_data.sector_status_by_close(daily)
                    self._sector_days = sorted(self._sector_closes)
            except Exception:
                pass
        if self.params.get("trend_mode", True):
            closing = prepared.loc[prepared["time"].map(self._is_session_close)]
            self._stock_closes = dict(zip(closing["time"].dt.date, closing["signal_close"]))
            self._update_trend_at_closes()

    def _set_causal_context(self, ts):
        day = ts.date()
        i = bisect_left(self._session_days, day)
        prior_session = self._session_days[i - 1] if i else None
        if i == len(self._session_days) or self._session_days[i] != day:
            insort(self._session_days, day)
        mode = self._mode_map.get(prior_session, "range")
        status = self._status_map.get(ts, "flat")
        sector_i = bisect_left(self._sector_days, day)
        if ts in self._status_map and sector_i:
            previous_sector_day = self._sector_days[sector_i - 1]
            # 缓存缺上一交易日时回到原指数状态，避免旧行业方向跨日反复生效。
            if prior_session is not None and previous_sector_day >= prior_session:
                sector = self._sector_closes[previous_sector_day]
                if sector == "weak" and status != "crash":
                    status = "weak"
                elif sector == "strong" and status in ("flat", "strong"):
                    status = "strong"
        if ts in self._status_map and mode == "defense" and status != "crash":
            status = "weak"
        self.engine.set_context(status, self._chg_map.get(ts, 0.0), mode)

    def _ingest_session_close(self, ts, bar):
        if (self.params.get("enable_sentiment_filter", True)
                and self.params.get("trend_mode", True)
                and self._is_session_close(ts) and ts not in self._indicator):
            self._stock_closes[ts.date()] = float(bar.get("signal_close", bar["close"]))
            self._update_trend_at_closes()

    def prepare(self, df: pd.DataFrame) -> None:
        if self._prepared:
            return
        p = self.params
        prepared, _events, _adj = prepare_signal_prices(df, self._code)
        prepared = prepared.sort_values("time", kind="stable").reset_index(drop=True)
        start = pd.Timestamp(prepared["time"].iloc[0]).strftime("%Y-%m-%d")
        end = pd.Timestamp(prepared["time"].iloc[-1]).strftime("%Y-%m-%d")
        if self.broker_driven:
            self._prepare_causal_context(prepared, start, end)
        else:
            self._prepare_reference_context(prepared, start, end)

        self._prepare_indicators(prepared)

    def _prepare_reference_context(self, prepared, start, end):
        p = self.params
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

    def _prepare_indicators(self, prepared):
        p = self.params
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
        if self.broker_driven:
            self._inc.warmup(prepared)
        else:
            replay_cache = IncrementalIndicatorCache(self.params)
            self.engine.replay_warmup(prepared, self.account, replay_cache)
        self._prepared = True

    def on_bar(self, bar: pd.Series, _broker) -> Iterable[StrategySignal]:
        ts = pd.Timestamp(bar["time"])
        if not self._prepared:
            raise RuntimeError("SentimentTSharedLiveEngine.prepare(df) 未调用")
        if self.broker_driven:
            self._set_causal_context(ts)
        else:
            self.engine.set_context(
                index_status=self._status_map.get(ts, "flat"),
                index_change_pct=self._chg_map.get(ts, 0.0),
                day_mode=self._mode_map.get(ts.date(), "range"))
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
        trade = (self._broker_trade(bar, _broker, indicator, vwap) if self.broker_driven
                 else self.engine.on_bar(bar, self.account, indicator, vwap))
        if self.broker_driven:
            self._ingest_session_close(ts, bar)
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
