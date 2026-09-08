# -*- coding: utf-8 -*-
"""均线波段严格增量信号引擎：仅使用已完成日线，下一交易日首根bar发单。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from core.ma_swing_signals import calculate_order_shares, shifted_execution_signals
from core.paper_models import OrderSide
from core.paper_replay import StrategySignal


@dataclass
class DailyBar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MaSwingLiveState:
    current_day: Optional[date] = None
    current_open: float = 0.0
    current_high: float = 0.0
    current_low: float = 0.0
    current_close: float = 0.0
    current_volume: float = 0.0
    completed: List[DailyBar] = field(default_factory=list)
    entry_day: Optional[date] = None
    highest_price: float = 0.0
    last_signal_day: Optional[date] = None
    processed_fill_count: int = 0

    def to_dict(self) -> dict:
        return {
            "current_day": self.current_day.isoformat() if self.current_day else None,
            "current_open": self.current_open,
            "current_high": self.current_high,
            "current_low": self.current_low,
            "current_close": self.current_close,
            "current_volume": self.current_volume,
            "completed": [
                {"day": item.day.isoformat(), "open": item.open, "high": item.high,
                 "low": item.low, "close": item.close, "volume": item.volume}
                for item in self.completed
            ],
            "entry_day": self.entry_day.isoformat() if self.entry_day else None,
            "highest_price": self.highest_price,
            "last_signal_day": self.last_signal_day.isoformat() if self.last_signal_day else None,
            "processed_fill_count": self.processed_fill_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MaSwingLiveState":
        return cls(
            current_day=date.fromisoformat(d["current_day"]) if d.get("current_day") else None,
            current_open=float(d.get("current_open", 0.0)),
            current_high=float(d.get("current_high", 0.0)),
            current_low=float(d.get("current_low", 0.0)),
            current_close=float(d.get("current_close", 0.0)),
            current_volume=float(d.get("current_volume", 0.0)),
            completed=[
                DailyBar(day=date.fromisoformat(item["day"]), open=float(item["open"]),
                         high=float(item["high"]), low=float(item["low"]),
                         close=float(item["close"]), volume=float(item["volume"]))
                for item in d.get("completed", [])
            ],
            entry_day=date.fromisoformat(d["entry_day"]) if d.get("entry_day") else None,
            highest_price=float(d.get("highest_price", 0.0)),
            last_signal_day=date.fromisoformat(d["last_signal_day"]) if d.get("last_signal_day") else None,
            processed_fill_count=int(d.get("processed_fill_count", 0)),
        )


class MaSwingLiveEngine:
    def __init__(self, symbol: str, params: Dict, strategy_id: str = "ma_swing",
                 prepared_df: Optional[pd.DataFrame] = None,
                 corporate_action_dates: Optional[List[date]] = None):
        self.symbol = symbol
        self.params = dict(params)
        self.warmup_end = self.params.pop("warmup_end", None)
        if self.warmup_end is not None:
            self.warmup_end = pd.Timestamp(self.warmup_end)
        self.strategy_id = strategy_id
        self.state = MaSwingLiveState()
        self.corporate_action_dates = set(corporate_action_dates or [])
        self.signal_price_by_time = {}
        if prepared_df is not None:
            prepared = prepared_df.copy()
            prepared["time"] = pd.to_datetime(prepared["time"])
            self.signal_price_by_time = {
                row["time"]: {
                    "open": float(row.get("signal_open", row["open"])),
                    "high": float(row.get("signal_high", row["high"])),
                    "low": float(row.get("signal_low", row["low"])),
                    "close": float(row.get("signal_close", row["close"])),
                }
                for _, row in prepared.iterrows()
            }

    def on_bar(self, bar: pd.Series, broker) -> Iterable[StrategySignal]:
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        day = ts.date()
        price = float(bar["close"])
        high = float(bar.get("high", price))
        low = float(bar.get("low", price))
        volume = float(bar.get("volume", 0) or 0)
        signal_prices = self.signal_price_by_time.get(pd.Timestamp(bar["time"]), {})
        signal_open = float(signal_prices.get("open", bar.get("open", price)))
        signal_high = float(signal_prices.get("high", high))
        signal_low = float(signal_prices.get("low", low))
        signal_close = float(signal_prices.get("close", price))
        signals = []
        self._sync_lifecycle_from_fills(broker)

        if self.state.current_day != day:
            if self.state.current_day is not None:
                self._finalize_current_day()
            self.state.current_day = day
            self.state.current_open = signal_open
            self.state.current_high = signal_high
            self.state.current_low = signal_low
            self.state.current_close = signal_close
            self.state.current_volume = volume
            # 快速回测在新交易日首根bar用真实开盘价作为执行参考；
            # 此前误用当根收盘价，导致买入数量、ATR止损和同bar再入场分叉。
            execution_price = float(bar.get("open", price))
            position = broker.account.positions.get(self.symbol)
            if position and position.quantity > 0:
                # 快速路径在信号判断前已把当根真实最高价纳入移动止损跟踪。
                self.state.highest_price = max(self.state.highest_price, high)
            # 预热段：只推进 completed 日线，不产生交易信号。
            if self.warmup_end is not None and pd.Timestamp(bar["time"]) < self.warmup_end:
                signals = []
            else:
                signals = self._signals_for_new_day(ts, execution_price, broker)
        else:
            self.state.current_high = max(self.state.current_high, signal_high)
            self.state.current_low = min(self.state.current_low, signal_low)
            self.state.current_close = signal_close
            self.state.current_volume += volume

        # 快速路径在每根分钟bar都用真实价高点更新移动止损；增量路径必须同样更新。
        position = broker.account.positions.get(self.symbol)
        if position and position.quantity > 0:
            self.state.highest_price = max(self.state.highest_price, high)
        return signals

    def _sync_lifecycle_from_fills(self, broker) -> None:
        """持仓生命周期只由实际成交推进，信号、拒单和未成交订单不得改状态。"""
        fills = broker.fills
        for fill in fills[self.state.processed_fill_count:]:
            if fill.symbol != self.symbol:
                continue
            fill_day = fill.filled_at.date()
            if fill.side == OrderSide.BUY:
                if self.state.entry_day is None:
                    self.state.entry_day = fill_day
                    self.state.highest_price = fill.price
                else:
                    self.state.highest_price = max(self.state.highest_price, fill.price)
            else:
                position = broker.account.positions.get(self.symbol)
                if position is None or position.quantity == 0:
                    self.state.entry_day = None
                    self.state.highest_price = 0.0
        self.state.processed_fill_count = len(fills)

    def _finalize_current_day(self) -> None:
        self.state.completed.append(DailyBar(
            day=self.state.current_day,
            open=self.state.current_open,
            high=self.state.current_high,
            low=self.state.current_low,
            close=self.state.current_close,
            volume=self.state.current_volume,
        ))

    def finalize_if_dirty(self) -> None:
        """预热结束后，把尚未 finalize 的最后一个交易日推进 completed。

        正常实时观察靠「下一交易日到来」触发 finalize；但预热段在最后一天结束，
        没有下一根 bar，故必须显式补 finalize，否则 MA60/ATR 少一根日线。
        """
        if self.state.current_day is not None and (
                not self.state.completed or self.state.completed[-1].day != self.state.current_day):
            self._finalize_current_day()

    def _daily_frame(self) -> pd.DataFrame:
        return pd.DataFrame([vars(item) for item in self.state.completed]).set_index("day")

    def _signals_for_new_day(self, ts: datetime, execution_price: float, broker) -> List[StrategySignal]:
        p = self.params
        slow = int(p.get("slow_ma", 60))
        # 公共信号在第 slow_ma 根完整日线即可产生有效趋势值；额外 +6 会漏掉首个合法信号。
        need = max(slow, int(p.get("breakout_days", 20)) + 1,
                   int(p.get("atr_period", 14)) + 1)
        if (len(self.state.completed) < need or self.state.last_signal_day == ts.date()
                or ts.date() in self.corporate_action_dates):
            return []
        daily = self._daily_frame().reset_index().rename(columns={"day": "time"})
        synthetic = pd.DataFrame([{
            "time": pd.Timestamp(ts.date()), "open": execution_price,
            "high": execution_price, "low": execution_price, "close": execution_price,
            "volume": 0.0,
        }])
        signals_table = shifted_execution_signals(pd.concat([daily, synthetic], ignore_index=True), p)
        sig = signals_table.loc[pd.Timestamp(ts.date())]
        trend = pd.notna(sig["trend_ok"]) and bool(sig["trend_ok"])
        pullback = pd.notna(sig["pullback_entry"]) and bool(sig["pullback_entry"])
        breakout = pd.notna(sig["breakout_entry"]) and bool(sig["breakout_entry"])

        position = broker.account.positions.get(self.symbol)
        quantity = position.quantity if position else 0
        signals = []
        if quantity == 0 and trend and (pullback or breakout):
            shares = calculate_order_shares(broker.account.cash, execution_price, p)
            if shares > 0:
                reason = "严格增量-趋势放量突破" if breakout else "严格增量-多头趋势回踩均线"
                signals.append(StrategySignal(self.strategy_id, self.symbol, OrderSide.BUY,
                                               shares, ts, reason))
        elif quantity > 0:
            atr_signal = float(sig["atr"])
            # 快速路径用当前执行bar的前复权signal_close换算ATR尺度；
            # 不能用上一完成日，否则跨除权/价格跳变时止损线会偏。
            current_signal_close = self.state.current_close
            scale = execution_price / current_signal_close if current_signal_close > 0 else 1.0
            atr = atr_signal * scale
            trend_exit = pd.notna(sig["trend_exit"]) and bool(sig["trend_exit"])
            held = (sum(self.state.entry_day <= item.day <= ts.date()
                        for item in self.state.completed) if self.state.entry_day else 0)
            atr_stop = self.state.highest_price - float(p.get("atr_stop_multiple", 2.5)) * atr
            reason = ""
            if execution_price <= atr_stop:
                reason = "严格增量-ATR移动止损"
            elif trend_exit:
                reason = "严格增量-连续跌破趋势线"
            elif held >= int(p.get("max_holding_days", 120)):
                reason = "严格增量-达到最大持仓期"
            if reason:
                signals.append(StrategySignal(self.strategy_id, self.symbol, OrderSide.SELL,
                                               quantity, ts, reason))
                # 生命周期状态等卖单实际成交后再清理；拒单、挂单和部分成交均保留。
                # 快速路径允许同一首根bar先卖后按当日有效信号重新买入。
                if trend and (pullback or breakout):
                    # 快速路径在同bar卖出后立即用回收现金计算再买入数量。
                    # 严格增量路径生成信号时尚未提交卖单，因此先按同一费用与滑点
                    # 口径预估卖出后可用现金，避免买入数量被旧现金压低。
                    sell_fill_price = execution_price * (
                        1 - float(p.get("slippage_bps", 5)) / 10000.0
                    )
                    sell_value = sell_fill_price * quantity
                    sell_fee = (
                        max(float(p.get("min_commission", 0.0)),
                            sell_value * float(p.get("commission_rate", 0.0001)))
                        + sell_value * float(p.get("stamp_tax_rate", 0.001))
                        + sell_value * float(p.get("transfer_fee_rate", 0.00002))
                    )
                    cash_after_sell = broker.account.cash + sell_value - sell_fee
                    shares = calculate_order_shares(cash_after_sell, execution_price, p)
                    if shares > 0:
                        entry_reason = ("严格增量-趋势放量突破" if breakout
                                        else "严格增量-多头趋势回踩均线")
                        signals.append(StrategySignal(self.strategy_id, self.symbol, OrderSide.BUY,
                                                       shares, ts, entry_reason))
        if signals:
            self.state.last_signal_day = ts.date()
        return signals
