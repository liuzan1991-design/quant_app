# -*- coding: utf-8 -*-
"""本地模拟撮合器：订单幂等、状态机、T+1持仓与成交量约束。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from traceback import format_exc as _format_exc
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
            # H9 观测增强（纯新增，不改任何交易行为）：风控拒绝此前是静默的，
            # 无法区分「策略无信号」与「信号被风控拒」。结构化拒绝原因写入审计日志，
            # 由 PaperStateStore.flush_new_audit() 持久化到 paper_audit.jsonl。
            self.audit_log.append({
                "time": now,
                "client_order_id": request.client_order_id,
                "event": "RISK_REJECTED",
                "side": request.side.value,
                "symbol": request.symbol,
                "quantity": request.quantity,
                "strategy_id": request.strategy_id,
                "violations": [item.code for item in decision.violations],
            })
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
        # H40：先应用到期除权除息事件（只读本地缓存，失败不阻断换日），再解锁持仓。
        # 顺序保证：事件应用后换日循环统一重置 sellable_quantity，送转股当日即可卖出。
        self._apply_due_corporate_actions(trading_day)
        self.account.trading_day = trading_day
        self.account.daily_realized_pnl = 0.0
        for position in self.account.positions.values():
            position.sellable_quantity = position.quantity
            position.today_bought_quantity = 0

    def apply_corporate_action(self, action, symbol: str) -> bool:
        """应用一次除权除息事件（H40，2026-10-21 拍板：税前入账留痕、成本冲减、幂等）。

        派息基数 = 登记日（送转前）持股；成本总价 = 原总成本 − 分红，送转摊薄每股。
        纯分红：cost −= dps（与 10-12 实测口径一致）；纯送转：cost ÷= (1+r)。
        同一 (symbol, ex_date) 只应用一次；无持仓也记账，防重复扫描。返回是否实际应用。
        """
        last = self.account.applied_corporate_actions.get(symbol, "")
        if action.ex_date <= last:
            return False
        self.account.applied_corporate_actions[symbol] = action.ex_date
        position = self.account.positions.get(symbol)
        if position is None or position.quantity <= 0:
            return False
        qty0 = position.quantity
        cash_div = qty0 * action.dividend_per_share
        total_cost = qty0 * position.average_cost
        new_qty = int(round(qty0 * (1.0 + action.bonus_ratio))) if action.bonus_ratio > 0 else qty0
        new_cost = (total_cost - cash_div) / new_qty
        position.quantity = new_qty
        position.average_cost = new_cost
        if cash_div > 0:
            self.account.cash += cash_div
        position.sellable_quantity = max(position.sellable_quantity, new_qty)
        self.audit_log.append({
            "time": datetime.now(),
            "event": "CORPORATE_ACTION",
            "symbol": symbol,
            "ex_date": action.ex_date,
            "plan": action.plan_desc,
            "dividend_per_share": action.dividend_per_share,
            "bonus_ratio": action.bonus_ratio,
            "cash_in": cash_div,
            "qty_before": qty0,
            "qty_after": new_qty,
            "cost_before": (total_cost / qty0) if qty0 else 0.0,
            "cost_after": new_cost,
        })
        return True

    def _apply_due_corporate_actions(self, trading_day: str) -> None:
        """换日钩子：对全部持仓标的应用到期除权事件。只读缓存，任何异常不阻断换日。"""
        try:
            from core import corporate_actions as _ca
            symbols = list(self.account.positions.keys()) or \
                list(self.account.applied_corporate_actions.keys())
            for symbol in symbols:
                code = _ca.symbol_to_code(symbol)
                last = self.account.applied_corporate_actions.get(symbol, "")
                position = self.account.positions.get(symbol)
                # H50（2026-10-23）：重放上界锚定建仓日。否则 applied 为空时会把
                # 缓存里建仓前的全部历史公司行为一次性重放到当前持仓（实测：100 股
                # 被重放成 636 股、权益虚高 29.7 万）。锚点未知 ⇒ 不重放（宁漏勿错）。
                if position is not None and not position.opened_date and not last:
                    self.audit_log.append({
                        "time": datetime.now(),
                        "event": "CORPORATE_ACTION_SKIPPED_NO_ANCHOR",
                        "symbol": symbol,
                        "trading_day": trading_day,
                    })
                    continue
                after = max(last, position.opened_date) if position is not None else last
                for action in _ca.pending_from_cache(code, after, trading_day):
                    self.apply_corporate_action(action, symbol)
        except Exception:  # noqa: BLE001 —— 记账修正不应阻断交易主流程；留审计痕迹
            self.audit_log.append({
                "time": datetime.now(),
                "event": "CORPORATE_ACTION_ERROR",
                "trading_day": trading_day,
                "error": _format_exc(),
            })

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
        position = self.account.positions.get(fill.symbol)
        if position is None:
            position = self.account.positions[fill.symbol] = Position(symbol=fill.symbol)
            # H50：首笔成交日期 = 建仓日，公司行为钩子以此为历史重放的上界锚点。
            position.opened_date = fill.filled_at.date().isoformat()
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
