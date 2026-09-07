# -*- coding: utf-8 -*-
"""离线分钟行情回放与策略信号到模拟订单的安全适配层。"""
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
        order_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return OrderRequest(
            client_order_id=order_id,
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            created_at=signal.signal_time,
            order_type=OrderType.LIMIT if signal.limit_price is not None else OrderType.MARKET,
            limit_price=signal.limit_price,
            strategy_id=signal.strategy_id,
        )


class OfflineReplayEngine:
    """按历史时间顺序逐根推送 K 线，使用 bar 时间作为逻辑时钟。"""

    def __init__(self, broker: PaperBroker, store: Optional[PaperStateStore] = None):
        self.broker = broker
        self.store = store
        self.processed_bars = 0
        self.signal_log = []

    @staticmethod
    def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
        required = {"time", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"行情缺少字段：{sorted(missing)}")
        bars = df.copy()
        bars["time"] = pd.to_datetime(bars["time"])
        bars = bars.sort_values("time").drop_duplicates("time", keep="last").reset_index(drop=True)
        if bars.empty:
            raise ValueError("回放行情为空")
        return bars

    def run(self, df: pd.DataFrame, symbol: str,
            signal_handler: Callable[[pd.Series, PaperBroker], Iterable[StrategySignal]],
            autosave_every: int = 0) -> PaperBroker:
        bars = self.normalize_bars(df)
        current_day = None
        for _, bar in bars.iterrows():
            ts = bar["time"].to_pydatetime()
            day = ts.date().isoformat()
            is_first_bar = day != current_day
            execution_reference = float(bar.get("open", bar["close"])) if is_first_bar else float(bar["close"])
            snapshot = MarketSnapshot(
                symbol=symbol,
                timestamp=ts,
                last_price=execution_reference,
                volume=max(0, int(bar.get("volume", 0) or 0)),
                is_suspended=bool(bar.get("is_suspended", False)),
                limit_up=(float(bar["limit_up"]) if "limit_up" in bar and pd.notna(bar["limit_up"]) else None),
                limit_down=(float(bar["limit_down"]) if "limit_down" in bar and pd.notna(bar["limit_down"]) else None),
            )
            self.broker.mark_to_market(snapshot)
            if day != current_day:
                self.broker.start_new_trading_day(day)
                current_day = day
            for order_id, order in list(self.broker.orders.items()):
                if order.request.symbol == symbol and order.remaining_quantity > 0:
                    self.broker.match_order(order_id, snapshot, ts)
            # 同一bar按信号顺序提交并立即撮合；前一笔成交先更新账户，后一笔再做风控。
            for signal in signal_handler(bar, self.broker) or ():
                if signal.symbol != symbol:
                    continue
                # 同bar卖出后立即再买入时，快速回测的 SimAccount 已同步回收现金；
                # PaperBroker 市价单在当前实现中会立即成交，这里显式用同一快照撮合，
                # 保证策略计算下一笔买入数量前账户现金已经更新。
                request = SignalOrderAdapter.to_order(signal)
                order = self.broker.submit_order(request, snapshot, ts)
                if order.status in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
                    self.broker.match_order(request.client_order_id, snapshot, ts)
                lifecycle_sync = getattr(signal_handler, "__self__", None)
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
            self.processed_bars += 1
            if self.store and autosave_every > 0 and self.processed_bars % autosave_every == 0:
                self.store.save(self.broker)
        if self.store:
            self.store.save(self.broker)
        return self.broker
