# -*- coding: utf-8 -*-
"""生产做T适配：共享引擎只试算意图，账户和策略成交状态以 broker 为准。"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from core.paper_models import OrderSide, OrderStatus


class BrokerTAccount:
    """提供共享引擎所需接口；buy/sell 不成交、不改账户。"""

    protect_base_position = True

    def __init__(self, broker, symbol, base_position, *, base_target=None, eligible_t=None):
        self.broker = broker
        self.symbol = symbol
        self.base_position = base_position
        self.base_target = base_position if base_target is None else base_target
        self._eligible_t = eligible_t
        self.last_fill_price = 0.0
        self.last_fee = 0.0
        self.trading_enabled = True

    @property
    def position(self):
        return self.broker.account.positions.get(self.symbol)

    @property
    def cash(self):
        return self.broker.account.cash

    @property
    def total(self):
        return self.position.quantity if self.position else 0

    @property
    def closeable(self):
        p = self.position
        return max(0, min(p.sellable_quantity, p.quantity - p.today_bought_quantity)) if p else 0

    def new_day(self):
        # 换日由 broker 负责，策略不能自行解锁当日买入。
        pass

    @property
    def sellable_t(self):
        if self._eligible_t is None:
            # 网格沿用现有库存口径。
            return min(self.closeable, max(0, self.total - self.base_position))
        return min(self.closeable, self._eligible_t)

    def buy(self, price, shares):
        lot = self.broker.risk_manager.config.board_lot
        self.last_fill_price = price
        return self.trading_enabled and shares > 0 and shares % lot == 0 and self.cash >= price * shares

    def sell(self, price, shares):
        lot = self.broker.risk_manager.config.board_lot
        allowed = min(shares, self.sellable_t)
        self.last_fill_price = price
        return max(0, int(allowed) // lot * lot) if self.trading_enabled else 0

    def equity(self, price):
        return self.broker.account.equity()


@dataclass
class InventoryLot:
    cost: float
    quantity: int
    protected: bool
    bought_on: str


def _order_reason(broker, fill):
    order = broker.orders.get(fill.client_order_id)
    return (order.request.reason or "") if order else ""


def filled_inventory(broker, symbol, base):
    """成交和除权事件按生效顺序重放，返回分开的底仓/T仓及调整后底仓目标。"""
    fills = [fill for fill in broker.fills if fill.symbol == symbol]
    actions = [event for event in broker.corporate_action_events if event["symbol"] == symbol]
    events = [(fill.filled_at, 1, fill) for fill in fills]
    events += [(datetime.fromisoformat(action["ex_date"]), 0, action) for action in actions]
    lots = []
    for _, kind, event in sorted(events, key=lambda row: row[:2]):
        if kind == 0 and lots:
            ratio = 1 + float(event["bonus_ratio"])
            dividend = float(event["dividend_per_share"])
            base = round(base * ratio)
            original = adjusted = 0
            for lot in lots:
                old_qty = lot.quantity
                original += old_qty
                # 按累计股数取整，确保所有lot股数之和与账户送转一致。
                qty = round(original * ratio) - adjusted
                adjusted += qty
                lot.cost = (lot.cost - dividend) * old_qty / qty
                lot.quantity = qty
        elif kind == 1 and event.side == OrderSide.BUY:
            # H78：资金目标只决定建仓意图的尺寸。用实际订单意图区分
            # 底仓和T仓；旧快照缺意图时保守保护，不能凭股数猜为T仓。
            is_t = _order_reason(broker, event).startswith("加权买入")
            cost = event.price + event.fee / event.quantity
            lots.append(InventoryLot(cost, event.quantity, not is_t,
                                     event.filled_at.date().isoformat()))
        elif kind == 1:
            remaining = event.quantity
            day = event.filled_at.date().isoformat()
            for protected in (False, True):
                for lot in lots:
                    if lot.protected == protected and lot.bought_on < day and remaining:
                        take = min(lot.quantity, remaining)
                        lot.quantity -= take
                        remaining -= take
            lots = [lot for lot in lots if lot.quantity]
    return fills, lots, base


class BrokerDrivenTMixin:
    """仅 broker_driven 模式启用；历史离线对照仍使用原路径。"""

    def _sync_lifecycle_from_fills(self, broker):
        if not self.broker_driven:
            return
        p = broker.account.positions.get(self.symbol)
        key = (id(broker), len(broker.fills), len(broker.corporate_action_events),
               broker.account.trading_day, p.quantity if p else 0, p.average_cost if p else 0.0)
        if getattr(self, "_fill_state_key", None) == key:
            return
        fills, lots, target = filled_inventory(broker, self.symbol, int(self.params.get("base_position", 1000)))
        total = p.quantity if p else 0
        # 成交历史与账户不闭合时，来源不明的持仓不进入自动卖出。
        t_inventory = ([lot for lot in lots if not lot.protected]
                       if sum(lot.quantity for lot in lots) == total else [])
        t_lots = [(lot.cost, lot.quantity) for lot in t_inventory]
        actual_t = sum(qty for _, qty in t_lots)
        eligible_t = sum(lot.quantity for lot in t_inventory
                         if lot.bought_on < broker.account.trading_day)
        self.account = BrokerTAccount(broker, self.symbol, total - actual_t,
                                      base_target=target, eligible_t=eligible_t)
        self._fill_state_key = key
        state = self.engine.state
        state.t_buy_queue = t_lots
        state.t_total_shares = actual_t
        state.t_avg_cost = sum(price * q for price, q in t_lots) / actual_t if actual_t else 0.0
        state.base_ready = target <= 0 or total > 0 or any(f.side == OrderSide.BUY for f in fills)

        today = [f for f in fills if f.filled_at.date().isoformat() == broker.account.trading_day]
        buys = [f for f in today if f.side == OrderSide.BUY]
        sells = [f for f in today if f.side == OrderSide.SELL]
        state.buy_count_today = len({f.client_order_id for f in buys})
        state.sell_count_today = len({f.client_order_id for f in sells})
        state.last_buy_time = max((f.filled_at for f in buys), default=None)
        state.last_sell_time = max((f.filled_at for f in sells), default=None)

        def reason(fill):
            return _order_reason(broker, fill)

        state.daily_stop_loss_count = len({f.client_order_id for f in sells
                                          if reason(f).startswith("T仓位止损")})
        prices = [f for f in today if reason(f).startswith("加权卖出")
                  or (f.side == OrderSide.BUY and "卖出回补" in reason(f))]
        state.sell_lowest_price = prices[-1].price if prices else 0.0

    def _broker_trade(self, bar, broker, indicator, vwap):
        day = bar["time"].date()
        if self.engine.state.current_day != day:
            self.engine.state.roll_day(self.engine.state.base_ready, preserve_t_cost=True)
            self.engine.state.current_day = day
            self._fill_state_key = None
        self._sync_lifecycle_from_fills(broker)
        # 未成交/部分成交委托占用持仓；等其结束，避免每分钟重复开仓/超卖。
        if any(o.request.symbol == self.symbol and o.status in
               (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN,
                OrderStatus.PENDING_SUBMIT) for o in broker.orders.values()):
            return None
        before = deepcopy(self.engine.state)
        try:
            trade = self.engine.on_bar(bar, self.account, indicator, vwap)
        except Exception:
            self.engine.state = before
            raise
        if trade:
            # 评分代码会先推进计数/成本。撤销试算，成交后再从真实 fills 重建。
            self.engine.state = before
        return trade
