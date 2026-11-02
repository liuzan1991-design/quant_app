# -*- coding: utf-8 -*-
"""生产做T适配：共享引擎只试算意图，账户和策略成交状态以 broker 为准。"""
from __future__ import annotations

from copy import deepcopy

from core.paper_models import OrderSide, OrderStatus


class BrokerTAccount:
    """提供共享引擎所需接口；buy/sell 不成交、不改账户。"""

    protect_base_position = True

    def __init__(self, broker, symbol, base_position):
        self.broker = broker
        self.symbol = symbol
        self.base_position = base_position
        self.last_fill_price = 0.0
        self.last_fee = 0.0

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

    def buy(self, price, shares):
        lot = self.broker.risk_manager.config.board_lot
        self.last_fill_price = price
        return shares > 0 and shares % lot == 0 and self.cash >= price * shares

    def sell(self, price, shares):
        lot = self.broker.risk_manager.config.board_lot
        allowed = min(shares, self.closeable, max(0, self.total - self.base_position))
        self.last_fill_price = price
        return max(0, int(allowed) // lot * lot)

    def equity(self, price):
        return self.broker.account.equity()


class BrokerDrivenTMixin:
    """仅 broker_driven 模式启用；历史离线对照仍使用原路径。"""

    def _sync_lifecycle_from_fills(self, broker):
        if not self.broker_driven:
            return
        self.account = BrokerTAccount(broker, self.symbol, int(self.params.get("base_position", 1000)))
        p = self.account.position
        key = (id(broker), len(broker.fills), broker.account.trading_day,
               self.account.total, p.average_cost if p else 0.0)
        if getattr(self, "_fill_state_key", None) == key:
            return
        self._fill_state_key = key
        state = self.engine.state
        fills = [f for f in broker.fills if f.symbol == self.symbol]
        base = self.account.base_position
        lots = []  # [含费单位成本, 股数, 是否底仓]
        for f in fills:
            if f.side == OrderSide.BUY:
                base_held = sum(q for _, q, is_base in lots if is_base)
                base_qty = min(f.quantity, max(0, base - base_held))
                unit_cost = f.price + f.fee / f.quantity
                if base_qty:
                    lots.append([unit_cost, base_qty, True])
                if f.quantity > base_qty:
                    lots.append([unit_cost, f.quantity - base_qty, False])
            else:
                remaining = f.quantity
                # 做T卖出先扣 T 仓；旧记录可能卖到底仓，也必须按实际成交扣除。
                for is_base in (False, True):
                    for lot in lots:
                        if lot[2] == is_base and remaining:
                            take = min(lot[1], remaining)
                            lot[1] -= take
                            remaining -= take
                lots = [lot for lot in lots if lot[1]]
        t_lots = [(price, qty) for price, qty, is_base in lots if not is_base]
        actual_t = max(0, self.account.total - base)
        if sum(q for _, q in t_lots) != actual_t:
            # 外部导入/公司行为可能改变股数；绝不沿用大于实际持仓的旧 T 台账。
            t_lots = [(p.average_cost, actual_t)] if actual_t and p else []
        state.t_buy_queue = t_lots
        state.t_total_shares = actual_t
        state.t_avg_cost = sum(price * q for price, q in t_lots) / actual_t if actual_t else 0.0
        state.base_ready = base <= 0 or self.account.total > 0 or any(f.side == OrderSide.BUY for f in fills)

        today = [f for f in fills if f.filled_at.date().isoformat() == broker.account.trading_day]
        buys = [f for f in today if f.side == OrderSide.BUY]
        sells = [f for f in today if f.side == OrderSide.SELL]
        state.buy_count_today = len({f.client_order_id for f in buys})
        state.sell_count_today = len({f.client_order_id for f in sells})
        state.last_buy_time = max((f.filled_at for f in buys), default=None)
        state.last_sell_time = max((f.filled_at for f in sells), default=None)

        def reason(fill):
            order = broker.orders.get(fill.client_order_id)
            return getattr(order.request, "reason", "") if order else ""

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
