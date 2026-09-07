# -*- coding: utf-8 -*-
"""日内做T严格增量信号引擎。

只使用当前bar及历史bar，不读取当日未来VWAP或未来指标。
本引擎用于离线回放和一致性验证，不连接行情服务或券商。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from core.paper_models import OrderSide
from core.paper_replay import StrategySignal


@dataclass
class IntradayTState:
    current_day: Optional[date] = None
    base_ready: bool = False
    buy_count_today: int = 0
    sell_count_today: int = 0
    last_buy_time: Optional[datetime] = None
    last_sell_time: Optional[datetime] = None
    t_buy_queue: deque = field(default_factory=deque)
    t_avg_cost: float = 0.0
    t_total_shares: int = 0
    daily_stop_loss_count: int = 0
    sell_lowest_price: float = 0.0
    afternoon_close_done: bool = False
    day_amount: float = 0.0
    day_volume: float = 0.0
    prev_day_amplitude: float = 3.0
    prev_day_high: float = 0.0
    prev_day_low: float = 0.0
    processed_fill_count: int = 0


class IntradayTLiveEngine:
    def __init__(self, symbol: str, params: Dict,
                 strategy_id: str = "intraday_t",
                 index_status_map: Optional[Dict] = None,
                 index_change_pct_map: Optional[Dict] = None,
                 mode_map: Optional[Dict] = None):
        self.symbol = symbol
        self.params = dict(params)
        self.strategy_id = strategy_id
        self.state = IntradayTState()
        self.index_status_map = index_status_map or {}
        self.index_change_pct_map = index_change_pct_map or {}
        self.mode_map = mode_map or {}
        self.bars = []

    def on_bar(self, bar: pd.Series, broker) -> Iterable[StrategySignal]:
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        day = ts.date()
        price = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])
        volume = float(bar.get("volume", 0) or 0)
        amount = float(bar.get("amount", 0) or 0)
        signals = []
        self._sync_fills(broker)

        if self.state.current_day != day:
            if self.state.current_day is not None:
                self._roll_day()
            self.state = IntradayTState(base_ready=self.state.base_ready)
            self.state.current_day = day
            # 快速路径在跨日重置state后继承base_ready，
            # prev_day_amplitude固定为默认3.0；为保持一致也必须重置为3.0。
            self.state.prev_day_amplitude = 3.0

        self.state.day_amount += amount
        self.state.day_volume += volume
        self.bars.append({"time": ts, "open": float(bar.get("open", price)),
                          "high": high, "low": low, "close": price,
                          "volume": volume, "amount": amount})

        # 建底仓：首日第一根bar，与快速路径一致。
        if not self.state.base_ready:
            shares = int(self.params.get("base_position", 1000))
            if shares > 0:
                signals.append(StrategySignal(self.strategy_id, self.symbol,
                                              OrderSide.BUY, shares, ts, "严格增量-建底仓"))
            self.state.base_ready = True
            return signals

        min_bars = int(self.params.get("min_bars", 10))
        if len(self.bars) < min_bars:
            return signals
        # 快速回测的指标窗口为 min_bars + 20 根跨日连续bar。
        window = self.bars[-(min_bars + 20):]

        p = self.params
        hhmm = ts.hour * 100 + ts.minute
        if self.state.day_volume <= 0:
            return signals
        vwap = self.state.day_amount / self.state.day_volume
        deviation = (price - vwap) / vwap * 100.0
        amp_factor = 0.0025 * self.state.prev_day_amplitude
        above_ma = price > vwap * (1 + amp_factor)
        below_ma = price < vwap * (1 - amp_factor)

        closes = np.asarray([item["close"] for item in window], dtype=np.float64)
        highs = np.asarray([item["high"] for item in window], dtype=np.float64)
        lows = np.asarray([item["low"] for item in window], dtype=np.float64)
        volumes = np.asarray([item["volume"] for item in window], dtype=np.float64)

        atr = self._atr(highs, lows, closes, 14)
        vol_pct = atr[-1] / closes[-1] * 100 if np.isfinite(atr[-1]) and closes[-1] else 0.0
        if vol_pct < 1.5:
            vol_level = "low"
        elif vol_pct < 3.5:
            vol_level = "normal"
        else:
            vol_level = "high"

        macd = self._macd_tail(closes, int(p.get("macd_fast", 5)),
                               int(p.get("macd_slow", 10)),
                               int(p.get("macd_signal", 3)))
        rsi_series = self._rsi(closes, 14)
        cur_rsi = float(rsi_series[-1]) if np.isfinite(rsi_series[-1]) else 50.0
        lookback = int(p.get("nine_turn_lookback", 3))
        sig_high, sig_low = self._nine_turn_tail(closes, lookback)

        stop_loss_cfg = p.get("stop_loss_pct", 0.018)
        if isinstance(stop_loss_cfg, dict):
            stop_loss = float(stop_loss_cfg.get(vol_level, 0.018))
        else:
            stop_loss = float(stop_loss_cfg)
        cooldown_sell = int(p.get("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1}).get(vol_level, 2))
        cooldown_buy = int(p.get("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7}).get(vol_level, 5))

        position = broker.account.positions.get(self.symbol)
        total_qty = position.quantity if position else 0
        closeable_qty = position.sellable_quantity if position else 0
        if position is not None:
            closeable_qty = max(0, closeable_qty - position.today_bought_quantity)
        base_position = int(p.get("base_position", 1000))

        # T仓止损
        if (self.state.t_total_shares > 0 and self.state.t_avg_cost > 0
                and (price - self.state.t_avg_cost) / self.state.t_avg_cost < -stop_loss
                and self.state.daily_stop_loss_count < int(p.get("max_daily_stop_loss", 1))):
            if self._cooldown_ok(ts, False, cooldown_sell):
                # 快速路径止损卖出数量受 acc.closeable 和实际持仓限制；
                # T仓台账可能因底仓兜底卖出与实际持仓不同步，不能直接作为委托数量。
                stop_shares = min(self.state.t_total_shares,
                                  max(0, closeable_qty - base_position))
                if stop_shares <= 0:
                    return signals
                self.state.daily_stop_loss_count += 1
                self._mark_order_time(ts, False)
                self.state.t_total_shares = 0
                self.state.t_avg_cost = 0.0
                self.state.t_buy_queue.clear()
                signals.append(StrategySignal(self.strategy_id, self.symbol, OrderSide.SELL,
                                              stop_shares, ts,
                                              f"严格增量-T仓位止损(>{stop_loss*100:.1f}%)"))

        # 尾盘平T仓
        if hhmm >= 1445 and bool(p.get("enable_afternoon_close", True)):
            t_shares = max(0, closeable_qty - base_position)
            # 快速路径只平“前一日及更早买入、当日仍持有”的T仓；
            # closeable_qty已剔除当日买入，所以这里不能把底仓误算成T仓。
            if t_shares > 0:
                t_pnl = 0.0
                if self.state.t_avg_cost > 0:
                    t_pnl = (price - self.state.t_avg_cost) / self.state.t_avg_cost * 100
                should_close = hhmm >= 1455 or t_pnl > 0 or t_pnl < -0.5
                if should_close and self._cooldown_ok(ts, False, cooldown_sell):
                    self._mark_order_time(ts, False)
                    self.state.t_total_shares = 0
                    self.state.t_avg_cost = 0.0
                    self.state.t_buy_queue.clear()
                    signals.append(StrategySignal(self.strategy_id, self.symbol, OrderSide.SELL,
                                                  t_shares, ts, "严格增量-尾盘平仓"))

        if above_ma:
            same_minute = (self.state.last_sell_time is not None
                           and (ts - self.state.last_sell_time).total_seconds() < 60)
            if (self.state.sell_count_today < int(p.get("max_sells_per_day", 10))
                    and not same_minute):
                should_sell, score = self._evaluate_sell(closes, volumes, macd, vol_level,
                                                         cur_rsi, sig_high, ts)
                # 快速路径在订单提交前检查 acc.closeable；PaperBroker的可卖数量
                # 在T+1约束下与之一致。无可卖持仓时不生成卖出信号。
                sellable = max(0, closeable_qty - base_position)
                if sellable <= 0:
                    sellable = min(max(0, total_qty - int(base_position / 2)), 2000)
                sell_shares = min(sellable, int(p.get("fixed_shares", 100)))
                if (should_sell and sell_shares > 0
                        and self._cooldown_ok(ts, False, cooldown_sell)):
                    self._mark_order_time(ts, False)
                    self.state.sell_count_today += 1
                    self.state.sell_lowest_price = price
                    self._remove_t_cost(min(sell_shares, self.state.t_total_shares))
                    signals.append(StrategySignal(self.strategy_id, self.symbol, OrderSide.SELL,
                                                  sell_shares, ts,
                                                  f"严格增量-加权卖出 评分={score}"))

        elif below_ma:
            buy_blocked = (
                hhmm >= 1430 or hhmm >= 1445
                or self.state.buy_count_today >= int(p.get("max_buys_per_day", 10))
                or (self.state.last_buy_time is not None
                    and (ts - self.state.last_buy_time).total_seconds() < 60)
                or total_qty >= int(p.get("max_position", 6000))
                or deviation > self._dynamic_vwap_threshold(vol_level, hhmm)
            )
            if not buy_blocked:
                should_buy, score = self._evaluate_buy(closes, volumes, macd, vol_level,
                                                       cur_rsi, sig_low, price)
                if should_buy and self._cooldown_ok(ts, True, cooldown_buy):
                    shares = int(p.get("fixed_shares", 100))
                    if shares > 0:
                        self._mark_order_time(ts, True)
                        self.state.buy_count_today += 1
                        self._update_t_cost(price, shares)
                        signals.append(StrategySignal(self.strategy_id, self.symbol, OrderSide.BUY,
                                                      shares, ts,
                                                      f"严格增量-加权买入 评分={score}"))
        return signals

    def _sync_fills(self, broker) -> None:
        # 当前订单状态由PaperBroker管理，策略本地状态在提交信号时即时推进。
        return

    def _roll_day(self) -> None:
        if self.bars:
            high = max(item["high"] for item in self.bars)
            low = min(item["low"] for item in self.bars)
            open_price = self.bars[0]["open"]
            self.state.prev_day_amplitude = (high - low) / open_price * 100 if open_price else 3.0

    @staticmethod
    def _ema(arr, period):
        out = np.full(len(arr), np.nan)
        if len(arr) < period:
            return out
        k = 2.0 / (period + 1.0)
        out[period - 1] = np.mean(arr[:period])
        for i in range(period, len(arr)):
            out[i] = arr[i] * k + out[i - 1] * (1 - k)
        return out

    def _macd_tail(self, closes, fast, slow, signal):
        dif = self._ema(closes, fast) - self._ema(closes, slow)
        valid = dif[~np.isnan(dif)]
        if not len(valid):
            return np.array([np.nan] * 6)
        dea = self._ema(valid, signal)
        dea_full = np.full(len(dif), np.nan)
        dea_full[len(dif) - len(dea):] = dea
        return (dif - dea_full) * 2.0

    @staticmethod
    def _rsi(closes, period=14):
        out = np.full(len(closes), np.nan)
        if len(closes) <= period:
            return out
        delta = np.diff(closes)
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        for i in range(period, len(delta)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        return out

    @staticmethod
    def _atr(highs, lows, closes, period=14):
        n = len(closes)
        out = np.full(n, np.nan)
        if n <= period:
            return out
        trs = np.zeros(n)
        trs[0] = highs[0] - lows[0]
        for i in range(1, n):
            trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]),
                         abs(lows[i] - closes[i-1]))
        out[period-1] = np.mean(trs[:period])
        for i in range(period, n):
            out[i] = (out[i-1] * (period-1) + trs[i]) / period
        return out

    @staticmethod
    def _nine_turn_tail(closes, lookback):
        n = len(closes)
        up = np.zeros(n, dtype=np.int64)
        down = np.zeros(n, dtype=np.int64)
        sig_high = sig_low = False
        if n < lookback + 2:
            return sig_high, sig_low
        for i in range(lookback, n):
            if closes[i] > closes[i-lookback]:
                up[i] = up[i-1] + 1
            elif closes[i] < closes[i-lookback]:
                down[i] = down[i-1] + 1
        sig_high = bool(8 <= up[-1] <= 9)
        sig_low = bool(8 <= down[-1] <= 9)
        return sig_high, sig_low

    @staticmethod
    def _dynamic_vwap_threshold(vol_level, hhmm):
        base = {"low": -0.2, "normal": -0.4, "high": -0.6}.get(vol_level, -0.4)
        if 930 <= hhmm <= 1030:
            base *= 0.7
        return max(base, -1.0)

    def _cooldown_ok(self, ts, is_buy, cooldown):
        last = self.state.last_buy_time if is_buy else self.state.last_sell_time
        return last is None or (ts - last).total_seconds() / 60.0 >= cooldown

    def _mark_order_time(self, ts, is_buy):
        if is_buy:
            self.state.last_buy_time = ts
        else:
            self.state.last_sell_time = ts

    def _evaluate_sell(self, closes, volumes, macd, vol_level, cur_rsi, sig_high, ts):
        p = self.params
        index_status = self.index_status_map.get(ts, "flat")
        index_change = float(self.index_change_pct_map.get(ts, 0.0))
        confirm = 2 if vol_level == "high" else 1
        score = 0
        score += 10 if sig_high else 0
        score += 2 if self._volume_price_divergence(closes, volumes, macd) else 0
        score += 2 if self._macd_top_divergence(closes, macd) else 0
        score += 1 if self._red_bar_shrinking(macd, confirm) else 0
        score += 2 if vol_level == "high" and cur_rsi > 70 else 0
        score += 1 if index_change > 0.5 and index_status == "strong" else 0
        return score >= int(p.get("sell_threshold", 7)), score

    def _evaluate_buy(self, closes, volumes, macd, vol_level, cur_rsi, sig_low, price):
        p = self.params
        confirm = 2 if vol_level == "high" else 1
        score = 0
        score += 10 if sig_low else 0
        score += 2 if self._panic_exhausted(closes, volumes) else 0
        score += 2 if self._green_bar_shrinking(macd, confirm) else 0
        score += 1 if cur_rsi < 25 else 0
        score += 1 if self._green_reliable(macd) else 0
        score += 3 if (self.state.sell_lowest_price > 0
                       and price <= self.state.sell_lowest_price * (1 - float(p.get("buy_back_gap_pct", 0.005)))) else 0
        return score >= int(p.get("buy_threshold", 6)), score

    @staticmethod
    def _volume_price_divergence(closes, volumes, macd):
        if len(closes) < 12:
            return False
        window = closes[-11:-1]
        if closes[-1] <= np.max(window):
            return False
        if len(volumes) >= 6 and volumes[-1] >= np.mean(volumes[-6:-1]):
            return False
        return len(macd) >= 3 and np.isfinite(macd[-1]) and macd[-1] > 0 and macd[-1] < macd[-2]

    @staticmethod
    def _macd_top_divergence(closes, macd, lookback=5):
        if len(closes) < lookback + 2 or len(macd) < lookback + 2:
            return False
        if closes[-1] <= np.max(closes[-lookback-1:-1]):
            return False
        return np.isfinite(macd[-1]) and macd[-1] < np.max(macd[-lookback-1:-1]) and macd[-1] > 0

    @staticmethod
    def _red_bar_shrinking(macd, confirm):
        if len(macd) < confirm + 2 or not np.isfinite(macd[-1]) or macd[-1] <= 0:
            return False
        for j in range(confirm):
            if macd[-1-j] >= macd[-2-j]:
                return False
        return True

    @staticmethod
    def _panic_exhausted(closes, volumes):
        if len(closes) < 8:
            return False
        if closes[-2] >= closes[-3]:
            return False
        if len(volumes) >= 7:
            avg = np.mean(volumes[-7:-2])
            if avg > 0 and volumes[-2] / avg >= 1.8 and volumes[-1] < np.mean(volumes[-6:-1]):
                return True
        return False

    @staticmethod
    def _green_bar_shrinking(macd, confirm):
        if len(macd) < confirm + 2 or not np.isfinite(macd[-1]) or macd[-1] >= 0:
            return False
        for j in range(confirm):
            if macd[-1-j] <= macd[-2-j]:
                return False
        return True

    @staticmethod
    def _green_reliable(macd):
        if len(macd) < 6:
            return False
        count = 0
        for value in reversed(macd):
            if value < 0:
                count += 1
            else:
                break
        return count >= 3

    def _update_t_cost(self, price, shares):
        total = self.state.t_avg_cost * self.state.t_total_shares + price * shares
        self.state.t_total_shares += shares
        self.state.t_avg_cost = total / self.state.t_total_shares if self.state.t_total_shares else 0.0
        self.state.t_buy_queue.append((price, shares))

    def _remove_t_cost(self, shares):
        remaining = shares
        while remaining > 0 and self.state.t_buy_queue:
            lot_price, lot_shares = self.state.t_buy_queue.popleft()
            if lot_shares <= remaining:
                remaining -= lot_shares
            else:
                self.state.t_buy_queue.appendleft((lot_price, lot_shares - remaining))
                remaining = 0
        total_shares = sum(sh for _, sh in self.state.t_buy_queue)
        total_cost = sum(price * sh for price, sh in self.state.t_buy_queue)
        self.state.t_total_shares = total_shares
        self.state.t_avg_cost = total_cost / total_shares if total_shares else 0.0
