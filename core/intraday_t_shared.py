# -*- coding: utf-8 -*-
"""日内做T共享状态机：快速路径与严格增量路径共用同一套状态与信号逻辑。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import pandas as pd

import backtest_generic as bg


@dataclass
class IntradayTSharedState:
    base_ready: bool = False
    buy_count_today: int = 0
    sell_count_today: int = 0
    last_buy_time: Optional[datetime] = None
    last_sell_time: Optional[datetime] = None
    t_buy_queue: list = field(default_factory=list)
    t_avg_cost: float = 0.0
    t_total_shares: int = 0
    daily_stop_loss_count: int = 0
    sell_lowest_price: float = 0.0
    afternoon_close_done: bool = False
    prev_day_amplitude: float = 3.0
    current_day: Optional[date] = None

    def roll_day(self, base_ready: bool) -> None:
        self.__dict__.update(IntradayTSharedState(base_ready=base_ready).__dict__)
        self.current_day = None


class IntradayTSharedEngine:
    """只负责信号与状态；成交账户由调用方传入并必须提供 buy/sell/equity 接口。"""

    def __init__(self, params: dict):
        self.params = dict(params)
        self.params.setdefault("min_bars", 10)
        self.params.setdefault("macd_fast", 5)
        self.params.setdefault("macd_slow", 10)
        self.params.setdefault("macd_signal", 3)
        self.params.setdefault("nine_turn_lookback", 3)
        self.params.setdefault("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
        self.params.setdefault("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
        self.params.setdefault("buy_back_gap_pct", 0.005)
        self.state = IntradayTSharedState()
        self._bar_index_status = "flat"
        self._bar_index_change_pct = 0.0
        self._day_mode = "range"

    def set_context(self, index_status="flat", index_change_pct=0.0, day_mode="range"):
        self._bar_index_status = index_status
        self._bar_index_change_pct = float(index_change_pct)
        self._day_mode = day_mode

    def on_bar(self, bar, account, indicator, vwap):
        """indicator: dict(macd_tail, rsi, vol_level, sig_high, sig_low)"""
        p = self.params
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        day = ts.date()
        if self.state.current_day != day:
            if self.state.current_day is not None and hasattr(account, "new_day"):
                account.new_day()
            self.state.roll_day(self.state.base_ready)
            self.state.current_day = day

        # 快速路径建底仓使用首日第一根bar收盘价；后续信号同样使用当前bar收盘价。
        price = float(bar["close"])
        hhmm = ts.hour * 100 + ts.minute
        base_position = int(p.get("base_position", 1000))
        fixed_shares = int(p.get("fixed_shares", 100))

        if not self.state.base_ready:
            ok = account.buy(price, base_position)
            if ok:
                self.state.base_ready = True
                return {
                    "direction": "BUY", "shares": base_position, "reason": "建底仓",
                    "price": account.last_fill_price, "fee": account.last_fee,
                }
            return None

        min_bars = int(p.get("min_bars", 10))
        if len(indicator.get("closes", [])) < min_bars or vwap <= 0:
            return None

        deviation = (price - vwap) / vwap * 100.0
        amp_factor = 0.0025 * self.state.prev_day_amplitude
        above_ma = price > vwap * (1 + amp_factor)
        below_ma = price < vwap * (1 - amp_factor)
        vol_level = indicator["vol_level"]
        stop_loss_cfg = p.get("stop_loss_pct", 0.018)
        if isinstance(stop_loss_cfg, dict):
            stop_loss = float(stop_loss_cfg.get(vol_level, 0.018))
        else:
            stop_loss = float(stop_loss_cfg)
        sell_map = p.get("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
        buy_map = p.get("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
        cooldown_sell = int(sell_map.get(vol_level, 2))
        cooldown_buy = int(buy_map.get(vol_level, 5))
        position = getattr(account, "closeable", getattr(account, "total", 0))

        if hhmm >= 1445 and bool(p.get("enable_afternoon_close", True)) and self._day_mode != "hold":
            t_shares = max(0, position - base_position)
            if t_shares > 0:
                t_pnl = ((price - self.state.t_avg_cost) / self.state.t_avg_cost * 100
                         if self.state.t_avg_cost > 0 else 0.0)
                should_close = hhmm >= 1455 or t_pnl > 0 or t_pnl < -0.5
                if should_close and self._cooldown_ok(ts, False, cooldown_sell):
                    sold = account.sell(price, t_shares)
                    if sold:
                        self.state.sell_count_today += 1
                        self.state.last_sell_time = ts
                        self._remove_t_cost(t_shares)
                        reason = ("尾盘强制平仓(14:55)" if hhmm >= 1455
                                  else f"尾盘止盈({t_pnl:+.2f}%)")
                        return {"direction": "SELL", "shares": sold, "reason": reason,
                                "price": account.last_fill_price, "fee": account.last_fee}
            if hhmm >= 1455:
                self.state.afternoon_close_done = True
                return None
            if hhmm < 1455 and not above_ma:
                return None

        if (self.state.t_total_shares > 0 and self.state.t_avg_cost > 0
                and (price - self.state.t_avg_cost) / self.state.t_avg_cost < -stop_loss
                and self.state.daily_stop_loss_count < int(p.get("max_daily_stop_loss", 1))):
            t_shares = self.state.t_total_shares
            if self._cooldown_ok(ts, False, cooldown_sell):
                sold = account.sell(price, t_shares)
                if sold:
                    self.state.sell_count_today += 1
                    self.state.daily_stop_loss_count += 1
                    self.state.last_sell_time = ts
                    self._remove_t_cost(t_shares)
                    reason = f"T仓位止损(>{stop_loss*100:.1f}%)"
                    return {"direction": "SELL", "shares": sold, "reason": reason,
                            "price": account.last_fill_price, "fee": account.last_fee}
            return None

        if above_ma:
            same_minute = (self.state.last_sell_time is not None
                           and (ts - self.state.last_sell_time).total_seconds() < 60)
            if (self.state.sell_count_today < int(p.get("max_sells_per_day", 10))
                    and not same_minute
                    and self._day_mode != "hold"):
                should_sell, score, details = self._evaluate_sell(indicator, ts)
                if should_sell:
                    sellable = max(0, position - base_position)
                    # 快速路径存在底仓兜底卖出：即使T仓不足，只要可卖持仓满足
                    # max(0, total - base/2) 仍可卖出，不能提前跳过。
                    if sellable <= 0:
                        sellable = min(max(0, account.total - int(base_position / 2)), 2000)
                    if sellable > 0:
                        sell_shares = min(sellable, fixed_shares)
                        if self._cooldown_ok(ts, False, cooldown_sell):
                            sold = account.sell(price, sell_shares)
                            if sold:
                                self.state.sell_count_today += 1
                                self.state.sell_lowest_price = price
                                self.state.last_sell_time = ts
                                self._remove_t_cost(min(sold, self.state.t_total_shares))
                                reason = f"加权卖出[{details}] 评分={score}"
                                return {"direction": "SELL", "shares": sold, "reason": reason,
                                        "price": account.last_fill_price, "fee": account.last_fee}

        elif below_ma:
            buy_blocked = (
                self._bar_index_status == "crash" or self._bar_index_change_pct < -1.5
                or (hhmm >= 1430 and self._bar_index_status not in ("strong",))
                or hhmm >= 1445
                or self.state.buy_count_today >= int(p.get("max_buys_per_day", 10))
                or (self.state.last_buy_time is not None
                    and (ts - self.state.last_buy_time).total_seconds() < 60)
                or account.total >= int(p.get("max_position", 6000))
                or deviation > self._dynamic_vwap_threshold(vol_level, hhmm)
            )
            if not buy_blocked:
                should_buy, score, details, raw_details = self._evaluate_buy(indicator, price)
                if should_buy and account.cash >= price * fixed_shares:
                    if self._cooldown_ok(ts, True, cooldown_buy):
                        ok = account.buy(price, fixed_shares)
                        if ok:
                            self.state.buy_count_today += 1
                            self.state.last_buy_time = ts
                            self._update_t_cost(account.last_fill_price, fixed_shares)
                            if raw_details.get("卖出回补", (0, False))[1]:
                                self.state.sell_lowest_price = price
                            reason = f"加权买入[{details}] 评分={score}"
                            return {"direction": "BUY", "shares": fixed_shares, "reason": reason,
                                    "price": account.last_fill_price, "fee": account.last_fee}
        return None

    def replay_warmup(self, df: pd.DataFrame, account, indicator_cache) -> None:
        """用历史数据推进引擎状态与账户到当前水位，不产生外部订单。

        indicator_cache 是独立的临时缓存，仅用于状态推进，不污染实时指标缓存。
        注意：本方法在 prepare 阶段配合临时缓存调用；实时增量前会另用预热好的缓存。
        """
        for _, row in df.iterrows():
            ts = pd.Timestamp(row["time"])
            signal_close = float(row.get("signal_close", row.get("close")))
            signal_high = float(row.get("signal_high", row.get("high")))
            signal_low = float(row.get("signal_low", row.get("low")))
            volume = float(row.get("volume", 0) or 0)
            amount = float(row.get("amount", 0) or 0)
            indicator, vwap = indicator_cache.push(
                ts, signal_close, signal_high, signal_low, volume, amount, volume)
            self.on_bar(row, account, indicator, vwap)

    def _cooldown_ok(self, ts, is_buy, cooldown):
        last = self.state.last_buy_time if is_buy else self.state.last_sell_time
        return last is None or (ts - last).total_seconds() / 60.0 >= cooldown

    @staticmethod
    def _dynamic_vwap_threshold(vol_level, hhmm):
        base = {"low": -0.2, "normal": -0.4, "high": -0.6}.get(vol_level, -0.4)
        if 930 <= hhmm <= 1030:
            base *= 0.7
        return max(base, -1.0)

    def _evaluate_sell(self, indicator, ts):
        import backtest_generic as bg
        p = self.params
        macd = indicator["macd_tail"]
        vol_level = indicator["vol_level"]
        confirm = 2 if vol_level == "high" else 1
        should, score, details = bg.evaluate_sell(
            indicator["closes"], indicator["volumes"], macd, confirm,
            indicator["sig_high"], vol_level, indicator["rsi"],
            float(self._bar_index_change_pct),
            self._bar_index_status, p)
        return should, score, bg.signal_summary(details)

    def _evaluate_buy(self, indicator, price):
        import backtest_generic as bg
        p = self.params
        macd = indicator["macd_tail"]
        vol_level = indicator["vol_level"]
        confirm = 2 if vol_level == "high" else 1
        should, score, details = bg.evaluate_buy(
            indicator["closes"], indicator["volumes"], macd, confirm,
            indicator["sig_low"], indicator["rsi"], self.state.sell_lowest_price,
            price, p)
        return should, score, bg.signal_summary(details), details

    def _update_t_cost(self, price, shares):
        total = self.state.t_avg_cost * self.state.t_total_shares + price * shares
        self.state.t_total_shares += shares
        self.state.t_avg_cost = total / self.state.t_total_shares if self.state.t_total_shares else 0.0
        self.state.t_buy_queue.append((price, shares))

    def _remove_t_cost(self, shares):
        remaining = shares
        while remaining > 0 and self.state.t_buy_queue:
            lot_price, lot_shares = self.state.t_buy_queue[0]
            take = min(lot_shares, remaining)
            remaining -= take
            if take == lot_shares:
                self.state.t_buy_queue.pop(0)
            else:
                self.state.t_buy_queue[0] = (lot_price, lot_shares - take)
        total_shares = sum(sh for _, sh in self.state.t_buy_queue)
        total_cost = sum(price * sh for price, sh in self.state.t_buy_queue)
        self.state.t_total_shares = total_shares
        self.state.t_avg_cost = total_cost / total_shares if total_shares else 0.0
