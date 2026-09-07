# -*- coding: utf-8 -*-
"""本地模拟撮合器：订单幂等、状态机、T+1持仓与成交量约束。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from uuid import uuid4

from core.paper_models import (
    AccountState,
    Fill,
    MarketSnapshot,
    Order,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TERMINAL_ORDER_STATUSES,
)
from core.risk_manager import RiskManager


@dataclass(frozen=True)
class PaperBrokerConfig:
    commission_rate: float = 0.0001
    min_commission: float = 0.0
    stamp_tax_rate: float = 0.001
    transfer_fee_rate: float = 0.00002
    slippage_bps: float = 5.0
    max_volume_participation: float = 0.10


_ALLOWED_TRANSITIONS = {
    OrderStatus.SIGNAL: {OrderStatus.PENDING_SUBMIT, OrderStatus.REJECTED},
    OrderStatus.PENDING_SUBMIT: {OrderStatus.SUBMITTED, OrderStatus.REJECTED},
    OrderStatus.SUBMITTED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                            OrderStatus.CANCELLED, OrderStatus.UNKNOWN},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
                                  OrderStatus.CANCELLED, OrderStatus.UNKNOWN},
    OrderStatus.UNKNOWN: {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED,
                          OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED},
}


class PaperBroker:
    """完全本地的模拟交易网关，不加载或调用任何真实券商 SDK。"""

    def __init__(self, initial_cash: float, trading_day: str,
                 risk_manager: Optional[RiskManager] = None,
                 config: Optional[PaperBrokerConfig] = None):
        self.account = AccountState(initial_cash=initial_cash, cash=initial_cash,
                                    trading_day=trading_day)
        self.risk_manager = risk_manager or RiskManager()
        self.config = config or PaperBrokerConfig()
        self.orders: Dict[str, Order] = {}
        self.fills: List[Fill] = []
        self.audit_log: List[dict] = []

    def _transition(self, order: Order, status: OrderStatus, now: datetime,
                    reason: str = "") -> None:
        if order.status in TERMINAL_ORDER_STATUSES:
            raise ValueError(f"终态订单不能迁移：{order.status.value} -> {status.value}")
        if status not in _ALLOWED_TRANSITIONS.get(order.status, set()):
            raise ValueError(f"非法订单状态迁移：{order.status.value} -> {status.value}")
        previous = order.status
        order.status = status
        order.updated_at = now
        if status == OrderStatus.REJECTED:
            order.reject_reason = reason
        self.audit_log.append({
            "time": now,
            "client_order_id": order.request.client_order_id,
            "event": "STATUS_CHANGED",
            "from": previous.value,
            "to": status.value,
            "reason": reason,
        })

    def submit_order(self, request: OrderRequest, market: MarketSnapshot,
                     now: datetime) -> Order:
        existing = self.orders.get(request.client_order_id)
        if existing is not None:
            self.audit_log.append({
                "time": now,
                "client_order_id": request.client_order_id,
                "event": "IDEMPOTENT_REPLAY",
            })
            return existing

        order = Order(request=request, updated_at=now)
        self.orders[request.client_order_id] = order
        decision = self.risk_manager.evaluate(request, self.account, market, now)
        if not decision.passed:
            self._transition(order, OrderStatus.REJECTED, now, decision.reason)
            return order
        self._transition(order, OrderStatus.PENDING_SUBMIT, now)
        self._transition(order, OrderStatus.SUBMITTED, now)
        self.match_order(request.client_order_id, market, now)
        return order

    def match_order(self, client_order_id: str, market: MarketSnapshot,
                    now: datetime) -> Order:
        order = self.orders[client_order_id]
        if order.status not in {OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED}:
            return order
        request = order.request
        if request.order_type == OrderType.LIMIT:
            if request.limit_price is None:
                self._transition(order, OrderStatus.UNKNOWN, now, "限价单缺少价格")
                return order
            executable = ((request.side == OrderSide.BUY and request.limit_price >= market.last_price)
                          or (request.side == OrderSide.SELL and request.limit_price <= market.last_price))
            if not executable:
                return order

        available = order.remaining_quantity
        if market.volume > 0:
            available = min(available, int(market.volume * self.config.max_volume_participation // 100 * 100))
        if request.side == OrderSide.SELL:
            position = self.account.positions.get(request.symbol)
            sellable = position.sellable_quantity if position else 0
            # T+1兜底：当日买入数量不能作为可卖数量。正常情况下
            # sellable_quantity在买入时不会增加，此检查用于防御状态恢复异常。
            if position is not None:
                sellable = max(0, sellable - position.today_bought_quantity)
            available = min(available, sellable)
        if available <= 0:
            return order

        slip = self.config.slippage_bps / 10000.0
        price = market.last_price * (1 + slip if request.side == OrderSide.BUY else 1 - slip)
        fee = self._calculate_fee(request.side, price, available)
        if request.side == OrderSide.BUY and price * available + fee > self.account.cash:
            affordable = int((self.account.cash / (price * (1 + self.config.commission_rate
                                                             + self.config.transfer_fee_rate))) // 100 * 100)
            available = min(available, affordable)
            if available <= 0:
                self._transition(order, OrderStatus.UNKNOWN, now, "撮合时可用资金不足")
                return order
            fee = self._calculate_fee(request.side, price, available)

        fill = Fill(fill_id=uuid4().hex, client_order_id=client_order_id,
                    symbol=request.symbol, side=request.side, quantity=available,
                    price=price, fee=fee, filled_at=now)
        self._apply_fill(fill)
        old_qty = order.filled_quantity
        order.filled_quantity += available
        order.average_fill_price = ((order.average_fill_price * old_qty + price * available)
                                    / order.filled_quantity)
        self.fills.append(fill)
        self.audit_log.append({
            "time": now,
            "client_order_id": client_order_id,
            "event": "FILL",
            "fill_id": fill.fill_id,
            "quantity": available,
            "price": price,
            "fee": fee,
        })
        status = OrderStatus.FILLED if order.remaining_quantity == 0 else OrderStatus.PARTIALLY_FILLED
        self._transition(order, status, now)
        return order

    def cancel_order(self, client_order_id: str, now: datetime) -> Order:
        order = self.orders[client_order_id]
        self._transition(order, OrderStatus.CANCELLED, now)
        return order

    def start_new_trading_day(self, trading_day: str) -> None:
        self.account.trading_day = trading_day
        self.account.daily_realized_pnl = 0.0
        for position in self.account.positions.values():
            position.sellable_quantity = position.quantity
            position.today_bought_quantity = 0

    def mark_to_market(self, market: MarketSnapshot) -> None:
        position = self.account.positions.get(market.symbol)
        if position:
            position.market_price = market.last_price

    def _calculate_fee(self, side: OrderSide, price: float, quantity: int) -> float:
        value = price * quantity
        commission = max(self.config.min_commission, value * self.config.commission_rate)
        transfer = value * self.config.transfer_fee_rate
        stamp = value * self.config.stamp_tax_rate if side == OrderSide.SELL else 0.0
        return commission + transfer + stamp

    def _apply_fill(self, fill: Fill) -> None:
        position = self.account.positions.setdefault(fill.symbol, Position(symbol=fill.symbol))
        position.market_price = fill.price
        value = fill.price * fill.quantity
        if fill.side == OrderSide.BUY:
            old_cost = position.average_cost * position.quantity
            self.account.cash -= value + fill.fee
            position.quantity += fill.quantity
            position.today_bought_quantity += fill.quantity
            # A股T+1：当日买入不可卖，但此前持仓仍可卖。初始建仓时sellable为0，
            # 之后start_new_trading_day会解锁全部持仓。
            position.average_cost = (old_cost + value + fill.fee) / position.quantity
            return

        realized = (fill.price - position.average_cost) * fill.quantity - fill.fee
        self.account.cash += value - fill.fee
        position.quantity -= fill.quantity
        position.sellable_quantity -= fill.quantity
        position.realized_pnl += realized
        self.account.daily_realized_pnl += realized
        self.account.realized_pnl_by_symbol[fill.symbol] = (
            self.account.realized_pnl_by_symbol.get(fill.symbol, 0.0) + realized
        )
        if position.quantity == 0:
            position.average_cost = 0.0
            position.market_price = fill.price
            position.today_bought_quantity = 0
