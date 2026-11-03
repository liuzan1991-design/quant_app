# -*- coding: utf-8 -*-
"""分钟回放：bar完成后决策，最早下一bar撮合；历史对照必须显式选择参考模式。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Optional

import pandas as pd

from core.paper_broker import PaperBroker
from core.paper_models import MarketSnapshot, OrderRequest, OrderSide, OrderStatus, OrderType
from core.paper_store import PaperStateStore


@dataclass(frozen=True)
class StrategySignal:
    strategy_id: str
    symbol: str
    side: OrderSide
    quantity: int
    signal_time: datetime
    reason: str = ""
    limit_price: Optional[float] = None


class SignalOrderAdapter:
    @staticmethod
    def to_order(signal: StrategySignal) -> OrderRequest:
        raw = (f"{signal.strategy_id}|{signal.symbol}|{signal.side.value}|"
               f"{signal.quantity}|{signal.signal_time.isoformat()}|{signal.reason}")
        return OrderRequest(
            client_order_id=hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24],
            symbol=signal.symbol, side=signal.side, quantity=signal.quantity,
            created_at=signal.signal_time,
            order_type=OrderType.LIMIT if signal.limit_price is not None else OrderType.MARKET,
            limit_price=signal.limit_price, strategy_id=signal.strategy_id, reason=signal.reason)


class OfflineReplayEngine:
    """下一bar开盘价是分钟模型的成交代理价，成交量只作为该分钟总容量上限。

    不保证在真实开盘打印价成交，不把已知收盘信号回填到同bar开盘。
    legacy_reference 仅用于历史算法一致性，不用于生产或经济有效性验证。
    """

    def __init__(self, broker: PaperBroker, store: Optional[PaperStateStore] = None,
                 gate: Optional[Callable] = None, *, execution_mode="next_bar", quote_now=None):
        if execution_mode not in {"next_bar", "legacy_reference"}:
            raise ValueError("execution_mode 必须为 next_bar 或 legacy_reference")
        self.broker, self.store, self.gate = broker, store, gate
        self.execution_mode = execution_mode
        self.latency_bars = 1
        self.quote_now = quote_now
        self._reference_day = None
        self.processed_bars = 0
        self.signal_log = []
        self.equity_curve = []

    def _own_shares(self, symbol):
        pos = self.broker.account.positions.get(symbol)
        return int(pos.quantity) if pos else 0

    @staticmethod
    def normalize_bars(df):
        missing = {"time", "close", "volume"} - set(df.columns)
        if missing:
            raise ValueError(f"行情缺少字段：{sorted(missing)}")
        bars = df.copy()
        bars["time"] = pd.to_datetime(bars["time"])
        bars = bars.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
        if bars.empty:
            raise ValueError("回放行情为空")
        return bars

    @staticmethod
    def _snapshot(bar, symbol, price):
        volume = bar.get("volume", 0)
        return MarketSnapshot(
            symbol=symbol, timestamp=pd.Timestamp(bar["time"]).to_pydatetime(),
            last_price=float(price), volume=max(0, int(volume)) if pd.notna(volume) else 0,
            is_suspended=bool(bar.get("is_suspended", False)),
            limit_up=float(bar["limit_up"]) if pd.notna(bar.get("limit_up")) else None,
            limit_down=float(bar["limit_down"]) if pd.notna(bar.get("limit_down")) else None)

    def _prepare_bar(self, bar, symbol):
        return bar

    def _drop_signal(self, signal):
        return False

    def _sync(self, handler):
        method = getattr(getattr(handler, "__self__", None), "_sync_lifecycle_from_fills", None)
        if callable(method):
            method(self.broker)

    def _record_signal(self, signal, order=None, reason=None, status=None):
        self.signal_log.append({
            "signal_time": signal.signal_time, "strategy_id": signal.strategy_id,
            "symbol": signal.symbol, "side": signal.side.value, "quantity": signal.quantity,
            "reason": reason if reason is not None else signal.reason,
            "client_order_id": order.request.client_order_id if order else "",
            "order_status": order.status.value if order else status,
            "filled_quantity": order.filled_quantity if order else 0})

    def begin_bar(self, bar, symbol):
        ts = pd.Timestamp(bar["time"]).to_pydatetime()
        causal = self.execution_mode == "next_bar"
        upto = self.broker.replay_upto.get(symbol) if causal else None
        if upto is not None and ts <= datetime.fromisoformat(upto):
            if ts == datetime.fromisoformat(upto):
                return None
            raise ValueError(f"行情早于账户已处理水位：{symbol} {ts} < {upto}")
        bar = self._prepare_bar(bar, symbol)
        day = ts.date().isoformat()
        if self.broker.account.trading_day != day:
            self.broker.start_new_trading_day(day)
        price = bar.get("open", bar["close"])
        if not causal and self._reference_day == day:
            price = bar["close"]
        self._reference_day = day
        snapshot = self._snapshot(bar, symbol, price)
        self.broker.mark_to_market(snapshot)
        return bar, snapshot

    def match_pending(self, snapshot):
        ts, symbol = snapshot.timestamp, snapshot.symbol
        for order_id, order in list(self.broker.orders.items()):
            if (order.request.symbol != symbol or order.status not in
                    {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}):
                continue
            if self.execution_mode == "next_bar" and ts <= order.request.created_at:
                continue
            if order.execution_wait_bars > 0:
                order.execution_wait_bars -= 1
                continue
            check = getattr(self.gate, "check_order", None)
            if order.request.side == OrderSide.BUY and callable(check) and not check(order, snapshot.last_price):
                self.broker.cancel_order(order_id, ts, "跨策略额度复核未通过")
                continue
            used = getattr(self.gate, "other_volume_used", None)
            self.broker.match_order(order_id, snapshot, ts, quote_now=self.quote_now,
                                    other_volume_used=used(symbol, ts) if callable(used) else 0)

    def mark_close(self, bar, symbol):
        snapshot = self._snapshot(bar, symbol, bar["close"])
        self.broker.mark_to_market(snapshot)
        return snapshot

    def finish_bar(self, bar, opening, closing, signal_handler):
        causal = self.execution_mode == "next_bar"
        ts, symbol = opening.timestamp, opening.symbol
        self._sync(signal_handler)
        for signal in signal_handler(bar, self.broker) or ():
            if signal.symbol != symbol:
                continue
            if signal.signal_time > ts:
                raise ValueError("信号时间晚于当前行情")
            if self._drop_signal(signal):
                self._record_signal(signal, status="DROPPED")
                continue
            decision = closing if causal else opening
            if (signal.side == OrderSide.BUY and self.gate is not None and not
                    self.gate(signal, decision.last_price, self._own_shares(symbol))):
                reason = getattr(self.gate, "last_rejection", "跨策略单标上限")
                self._record_signal(signal, reason=f"{signal.reason}|{reason}拒绝",
                                    status=OrderStatus.REJECTED.value)
                continue
            order = self.broker.submit_order(
                SignalOrderAdapter.to_order(signal), decision, ts,
                execute_immediately=not causal, quote_now=self.quote_now,
                execution_wait_bars=max(0, self.latency_bars - 1) if causal else 0)
            self._sync(signal_handler)
            self._record_signal(signal, order)
        self.broker.mark_to_market(closing)
        if causal:
            self.broker.replay_upto[symbol] = ts.isoformat()
        self.equity_curve.append({"time": ts, "equity": self.broker.account.equity()})
        self.processed_bars += 1

    def run(self, df, symbol, signal_handler, autosave_every=0):
        self._reference_day = None
        for _, row in self.normalize_bars(df).iterrows():
            started = self.begin_bar(row, symbol)
            if started is None:
                continue
            bar, opening = started
            self.match_pending(opening)
            closing = self.mark_close(bar, symbol)
            self.finish_bar(bar, opening, closing, signal_handler)
            if self.store and autosave_every > 0 and self.processed_bars % autosave_every == 0:
                self.store.save(self.broker)
        if self.store:
            self.store.save(self.broker)
        return self.broker
