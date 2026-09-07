# -*- coding: utf-8 -*-
"""移动网格共享状态机：快速路径与严格增量路径共用同一套逐bar状态与信号逻辑。

与 `strategies/grid_trade.py` 的快速路径保持口径一致：
- 盘中 VWAP 用当日累计量额比（截至当前bar，无未来数据）；
- 趋势防御同样只使用已完成交易日，但因为快速路径用 `pd.Timestamp(day)` 去查
  `date` 键字典，实际永远拿不到（False），这里显式复刻该行为以保证逐笔对齐。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class GridSharedState:
    base_price: Optional[float] = None
    anchor_price: Optional[float] = None
    grid_bought: int = 0
    stopped: bool = False
    prev_day: Optional[date] = None
    day_amount: float = 0.0
    day_volume: float = 0.0
    prev_day_close: float = 0.0
    daily_closes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "base_price": self.base_price,
            "anchor_price": self.anchor_price,
            "grid_bought": self.grid_bought,
            "stopped": self.stopped,
            "prev_day": self.prev_day.isoformat() if self.prev_day else None,
            "day_amount": self.day_amount,
            "day_volume": self.day_volume,
            "prev_day_close": self.prev_day_close,
            "daily_closes": self.daily_closes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GridSharedState":
        return cls(
            base_price=d.get("base_price"),
            anchor_price=d.get("anchor_price"),
            grid_bought=int(d.get("grid_bought", 0)),
            stopped=bool(d.get("stopped", False)),
            prev_day=date.fromisoformat(d["prev_day"]) if d.get("prev_day") else None,
            day_amount=float(d.get("day_amount", 0.0)),
            day_volume=float(d.get("day_volume", 0.0)),
            prev_day_close=float(d.get("prev_day_close", 0.0)),
            daily_closes=list(d.get("daily_closes", [])),
        )


class GridSharedEngine:
    """只负责信号与状态；成交账户由调用方传入并必须提供 buy/sell/new_day 接口。"""

    def __init__(self, params: dict):
        self.params = dict(params)
        self.state = GridSharedState()
        self._base_position = int(params.get("base_position", 1000))
        self._trade_shares = int(params.get("trade_shares", 100))
        self._grid_step = float(params.get("grid_pct", 2.0)) / 100.0
        self._grid_levels = int(params.get("grid_levels", 5))
        self._max_position = int(params.get("max_position", 6000))
        self._enable_close = bool(params.get("enable_afternoon_close", False))
        self._freeze_pct = float(params.get("center_freeze_pct", 8.0)) / 100.0
        self._hard_stop_pct = float(params.get("hard_stop_pct", 12.0)) / 100.0
        self._max_loss_pct = float(params.get("max_grid_loss_pct", 5.0)) / 100.0
        self._init_cash = float(params.get("_init_cash", 1_000_000.0))
        self._trend_ma = int(params.get("trend_ma", 60))

    def on_bar(self, bar, account):
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        day = ts.date()
        cur = float(bar["close"])
        signal_close = float(bar.get("signal_close", float(bar["close"])))
        hhmm = ts.hour * 100 + ts.minute
        trades = []

        if day != self.state.prev_day:
            if self.state.prev_day is not None:
                self.state.daily_closes.append(self.state.prev_day_close)
            if hasattr(account, "new_day"):
                account.new_day()
            self.state.day_amount = 0.0
            self.state.day_volume = 0.0
            self.state.prev_day = day

        self.state.day_amount += float(bar.get("amount", 0) or 0)
        self.state.day_volume += float(bar.get("volume", 0) or 0)
        self.state.prev_day_close = signal_close

        # 建底仓（首日第一根bar）
        if self.state.base_price is None:
            if self.state.anchor_price is None:
                self.state.anchor_price = cur
            if self._base_position > 0:
                ok = account.buy(cur, self._base_position)
                if ok:
                    self.state.base_price = account.last_fill_price
                    trades.append(self._trade(ts, "BUY", self._base_position,
                                              "建底仓", account))
            else:
                self.state.base_price = cur
            return trades or None

        raw_center = self.state.day_amount / self.state.day_volume if self.state.day_volume else np.nan
        if np.isnan(raw_center):
            raw_center = cur
        center_floor = self.state.anchor_price * (1 - self._freeze_pct)
        self.state.base_price = max(raw_center, center_floor)

        account_loss = account.equity(cur) / self._init_cash - 1
        hard_stop = cur <= self.state.base_price * (1 - self._hard_stop_pct)
        loss_stop = account_loss <= -self._max_loss_pct
        if hard_stop or loss_stop:
            self.state.stopped = True
            sellable_t = max(0, min(account.closeable, account.total - self._base_position))
            if sellable_t > 0:
                sold = account.sell(cur, sellable_t)
                if sold:
                    trades.append(self._trade(ts, "SELL", sold, "网格硬止损", account))
                    self.state.grid_bought = max(
                        0, self.state.grid_bought - sold // max(self._trade_shares, 1))

        # 尾盘平T仓（可选）
        if self._enable_close and hhmm >= 1455:
            t_shares = max(0, account.total - self._base_position)
            if t_shares > 0:
                sold = account.sell(cur, t_shares)
                if sold:
                    trades.append(self._trade(ts, "SELL", sold, "尾盘平T仓", account))
                    self.state.grid_bought = 0
            return trades or None

        # 卖出：涨回上一格
        if self.state.grid_bought > 0:
            sell_line = self.state.base_price * (1 - self._grid_step * (self.state.grid_bought - 1))
            if cur >= sell_line:
                sellable = max(0, account.total - self._base_position)
                if sellable >= self._trade_shares:
                    sold = account.sell(cur, self._trade_shares)
                    if sold:
                        trades.append(self._trade(
                            ts, "SELL", self._trade_shares,
                            f"网格卖出(回到{self.state.grid_bought - 1}档)", account))
                        self.state.grid_bought -= 1
                else:
                    trades.append(self._rejected(
                        ts, "SELL", self._trade_shares,
                        f"网格卖出但T仓不足(T+1/持仓{account.closeable})"))

        # 买入：跌破下一格
        # 趋势防御：MA60 向下（收盘 < MA60 且斜率向下）时暂停新买入，
        # 与快速路径 grid_trade.py 的 trend_defense.shift(1) 语义一致。
        defense = self._trend_defense_active()
        if (self.state.grid_bought < self._grid_levels
                and not self.state.stopped and not defense):
            buy_line = self.state.base_price * (1 - self._grid_step * (self.state.grid_bought + 1))
            if cur <= buy_line:
                if account.total + self._trade_shares > self._max_position:
                    trades.append(self._rejected(
                        ts, "BUY", self._trade_shares, "网格买入超最大持仓"))
                elif account.cash < cur * self._trade_shares:
                    trades.append(self._rejected(
                        ts, "BUY", self._trade_shares, "网格买入现金不足"))
                else:
                    ok = account.buy(cur, self._trade_shares)
                    if ok:
                        trades.append(self._trade(
                            ts, "BUY", self._trade_shares,
                            f"网格买入(-{self.state.grid_bought + 1}档)", account))
                        self.state.grid_bought += 1
                    else:
                        trades.append(self._rejected(
                            ts, "BUY", self._trade_shares, "网格买入未成交"))
        return trades or None

    def _trend_defense_active(self) -> bool:
        """趋势防御：与快速路径 trend_defense.shift(1) 语义一致。

        快速路径用「前一交易日的收盘 < 前一日 MA(trend_ma) 且 MA 斜率(5)向下」判定，
        当前 bar 前已完成的交易日收盘存放在 self.state.daily_closes（升序、含前一日）。
        """
        closes = self.state.daily_closes
        n = len(closes)
        if n < self._trend_ma:
            return False
        s = pd.Series(closes, dtype=float)
        ma = s.rolling(self._trend_ma, min_periods=self._trend_ma).mean()
        slope = ma.diff(5)
        last_close = s.iloc[-1]
        last_ma = ma.iloc[-1]
        last_slope = slope.iloc[-1]
        if pd.isna(last_ma) or pd.isna(last_slope):
            return False
        return bool(last_close < last_ma and last_slope < 0)

    @staticmethod
    def _trade(ts, direction, shares, reason, account):
        return {
            "time": ts, "direction": direction, "shares": shares, "reason": reason,
            "price": account.last_fill_price, "fee": account.last_fee,
        }

    @staticmethod
    def _rejected(ts, direction, shares, reason):
        return {
            "time": ts, "direction": direction, "shares": shares, "reason": reason,
            "rejected": True,
        }