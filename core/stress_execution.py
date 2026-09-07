# -*- coding: utf-8 -*-
"""执行层压力回放引擎。

在既有 OfflineReplayEngine 之外叠加执行层压力，不改任何策略信号逻辑：
- drop_rate：每个信号独立以固定概率丢弃，不重试；
- latency_bars：信号延迟 K 根 bar 后按该根 bar 开盘价撮合，撮合前重新走资金/持仓风控；
- apply_limit：用前收盘价近似估算涨跌停价（主板10%，创业板/科创板20%）。

所有维度默认关闭；关闭时必须回到原 OfflineReplayEngine 的结果。
"""
from __future__ import annotations

import random
from typing import Iterable, Optional

import pandas as pd

from core.paper_models import MarketSnapshot, OrderSide, OrderStatus
from core.paper_replay import OfflineReplayEngine, SignalOrderAdapter, StrategySignal


class StressExecutionEngine(OfflineReplayEngine):
    def __init__(self, broker, store=None, *,
                 drop_rate: float = 0.0,
                 latency_bars: int = 0,
                 apply_limit: bool = False,
                 seed: int = 42):
        super().__init__(broker, store)
        self.drop_rate = drop_rate
        self.latency_bars = latency_bars
        self.apply_limit = apply_limit
        self.seed = seed
        self.equity_curve = []

    def _limit_pct(self, symbol: str) -> float:
        return 0.20 if symbol.startswith(("30", "68")) else 0.10

    def _snapshot(self, bar, symbol: str, prev_close: Optional[float]) -> MarketSnapshot:
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        limit_up = None
        limit_down = None
        if self.apply_limit and prev_close is not None and prev_close > 0:
            pct = self._limit_pct(symbol)
            limit_up = round(prev_close * (1 + pct), 4)
            limit_down = round(prev_close * (1 - pct), 4)
        return MarketSnapshot(
            symbol=symbol,
            timestamp=ts,
            last_price=float(bar.get("close", bar["close"])),
            volume=max(0, int(bar.get("volume", 0) or 0)),
            is_suspended=bool(bar.get("is_suspended", False)),
            limit_up=limit_up,
            limit_down=limit_down,
        )

    def _submit(self, signal: StrategySignal, symbol: str, snapshot: MarketSnapshot,
                ts) -> None:
        if signal.symbol != symbol:
            return
        request = SignalOrderAdapter.to_order(signal)
        order = self.broker.submit_order(request, snapshot, ts)
        if order.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
            self.broker.match_order(request.client_order_id, snapshot, ts)
        lifecycle_sync = getattr(self._signal_handler, "__self__", None)
        sync_method = getattr(lifecycle_sync, "_sync_lifecycle_from_fills", None)
        if callable(sync_method):
            sync_method(self.broker)
        self.signal_log.append({
            "signal_time": signal.signal_time,
            "strategy_id": signal.strategy_id,
            "symbol": signal.symbol,
            "side": signal.side.value,
            "quantity": signal.quantity,
            "reason": signal.reason,
            "client_order_id": request.client_order_id,
            "order_status": order.status.value,
            "filled_quantity": order.filled_quantity,
        })

    def run(self, df: pd.DataFrame, symbol: str, signal_handler,
            autosave_every: int = 0):
        self._signal_handler = signal_handler
        bars = self.normalize_bars(df)
        rng = random.Random(self.seed)
        pending = []  # (ready_index, signal)
        prev_day_close = None
        current_day = None
        day_last_close = None
        self.equity_curve = []

        for idx, (_, bar) in enumerate(bars.iterrows()):
            ts = bar["time"].to_pydatetime()
            day = ts.date().isoformat()
            close = float(bar.get("close", bar["close"]))
            is_new_day = day != current_day
            if is_new_day and current_day is not None:
                prev_day_close = day_last_close

            snap = self._snapshot(bar, symbol, prev_day_close)
            self.broker.mark_to_market(snap)
            if is_new_day:
                self.broker.start_new_trading_day(day)
                current_day = day
            day_last_close = close

            for order_id, order in list(self.broker.orders.items()):
                if order.request.symbol == symbol and order.remaining_quantity > 0:
                    self.broker.match_order(order_id, snap, ts)

            # 延迟到期的单，按当前 bar 开盘价撮合并重新过风控
            if self.latency_bars > 0 and pending:
                ready = [p for p in pending if p[0] <= idx]
                pending = [p for p in pending if p[0] > idx]
                if ready:
                    open_bar = bar.copy()
                    open_bar["close"] = open_bar.get("open", open_bar["close"])
                    open_snap = self._snapshot(open_bar, symbol, prev_day_close)
                    for _, signal in ready:
                        self._submit(signal, symbol, open_snap, ts)

            for signal in signal_handler(bar, self.broker) or ():
                if signal.symbol != symbol:
                    continue
                if rng.random() < self.drop_rate:
                    self.signal_log.append({
                        "signal_time": signal.signal_time,
                        "strategy_id": signal.strategy_id,
                        "symbol": signal.symbol,
                        "side": signal.side.value,
                        "quantity": signal.quantity,
                        "reason": signal.reason,
                        "client_order_id": "",
                        "order_status": "DROPPED",
                        "filled_quantity": 0,
                    })
                    continue
                if self.latency_bars > 0:
                    pending.append((idx + self.latency_bars, signal))
                else:
                    self._submit(signal, symbol, snap, ts)

            self.equity_curve.append({
                "time": ts,
                "equity": self.broker.account.equity(),
            })
            self.processed_bars += 1
            if self.store and autosave_every > 0 and self.processed_bars % autosave_every == 0:
                self.store.save(self.broker)

        if pending:
            last_bar = bars.iloc[-1]
            last_ts = last_bar["time"].to_pydatetime()
            open_bar = last_bar.copy()
            open_bar["close"] = open_bar.get("open", open_bar["close"])
            open_snap = self._snapshot(open_bar, symbol, prev_day_close)
            for _, signal in pending:
                self._submit(signal, symbol, open_snap, last_ts)

        if self.store:
            self.store.save(self.broker)
        return self.broker