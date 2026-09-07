# -*- coding: utf-8 -*-
"""模拟盘统一数据模型。仅用于本地撮合，不包含任何真实券商接口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    SIGNAL = "SIGNAL"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
}


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    created_at: datetime
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    strategy_id: str = ""


@dataclass
class Order:
    request: OrderRequest
    status: OrderStatus = OrderStatus.SIGNAL
    filled_quantity: int = 0
    average_fill_price: float = 0.0
    reject_reason: str = ""
    updated_at: Optional[datetime] = None

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.request.quantity - self.filled_quantity)


@dataclass(frozen=True)
class Fill:
    fill_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    fee: float
    filled_at: datetime


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    sellable_quantity: int = 0
    average_cost: float = 0.0
    market_price: float = 0.0
    today_bought_quantity: int = 0
    realized_pnl: float = 0.0

    @property
    def market_value(self) -> float:
        return self.quantity * self.market_price


@dataclass
class AccountState:
    initial_cash: float
    cash: float
    trading_day: str
    positions: Dict[str, Position] = field(default_factory=dict)
    realized_pnl_by_symbol: Dict[str, float] = field(default_factory=dict)
    daily_realized_pnl: float = 0.0
    consecutive_stop_losses: Dict[str, int] = field(default_factory=dict)
    symbol_pause_until: Dict[str, str] = field(default_factory=dict)

    def equity(self) -> float:
        return self.cash + sum(position.market_value for position in self.positions.values())

    def position_value(self, symbol: str) -> float:
        position = self.positions.get(symbol)
        return position.market_value if position else 0.0


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    timestamp: datetime
    last_price: float
    volume: int = 0
    is_suspended: bool = False
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None


@dataclass(frozen=True)
class RiskViolation:
    code: str
    message: str
    severity: str = "blocker"
    value: Optional[float] = None
    limit: Optional[float] = None
    symbol: str = ""


@dataclass(frozen=True)
class RiskDecision:
    passed: bool
    violations: List[RiskViolation] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "；".join(item.message for item in self.violations)
