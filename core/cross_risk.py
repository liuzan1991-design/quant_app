# -*- coding: utf-8 -*-
"""H73：跨策略总仓位闸门（接实盘前必需，默认关闭）。

为什么需要
----------
纸面阶段每个「标的 × 策略」各有**一个独立 PaperBroker**，各自拿 `initial_cash`
（30 万）。四策略跑同一只 301717 ⇒ 纸面上是 **4×30 万 = 120 万**在买同一只票，
单标上限 25% 也被放大成 4×25% = 100%。而实盘是**同一个账户、同一笔 30 万**，
四个策略的买单会叠加到同一笔钱上。不装这个闸门，纸面成绩与实盘结果不可比。

设计
----
- **默认关闭**（`cross_symbol_cap is None` ⇒ `allowed_buy()` 返回 None ⇒ 不拦截），
  与 H57 的"无配置 ⇒ 行为逐字节不变"同纪律；
- 只拦**买入**：卖出永远放行（减仓不构成风险）；
- 台账按 `symbol → {observer_key: shares}` 记，判定时用「除自己之外」的持仓，
  避免把自己已持有的部分算两遍；
- 拒绝是**整单拒绝**（不做部分成交）—— 保守，且原因可追溯。

已知取舍
--------
观察者是**顺序**处理的，同一轮内先跑的策略先占额度。轮与轮之间只有几根新 bar，
偏差很小；但这是"先到先得"，不是按信号强度排队。
"""
from __future__ import annotations

from core.paper_models import OrderSide


class CrossSymbolLedger:
    """跨策略同标的持仓台账 + 买入额度判定。"""

    def __init__(self, cap_pct: float | None, base_cash: float, *,
                 max_account_position_pct: float = 1.0, max_daily_loss_pct: float = 1.0) -> None:
        self.cap_pct = None if cap_pct is None else float(cap_pct)
        self.base_cash = float(base_cash)
        self._hold: dict[str, dict[str, int]] = {}
        self.rejects: list[dict] = []
        self._brokers = {}
        self.max_account_position_pct = max_account_position_pct
        self.max_daily_loss_pct = max_daily_loss_pct

    def bind(self, key, broker):
        """单进程事件循环内直接读取订单账，准入与注册间没有异步间隙。

        重启从持久化订单重建冻结额，不另写一份易失配的预约账。
        """
        self._brokers[key] = broker
        for symbol, pos in broker.account.positions.items():
            self.update(key, symbol, pos.quantity)

    def _pending(self, symbol, price, exclude_key="", exclude_id=""):
        symbol_qty = 0
        total_value = cash = 0.0
        for key, broker in self._brokers.items():
            for order in broker.pending_orders(exclude_id if key == exclude_key else ""):
                if order.request.side != OrderSide.BUY:
                    continue
                r = order.request
                pos = broker.account.positions.get(r.symbol)
                ref = max(order.reserved_price, float(r.limit_price or 0),
                          price if r.symbol == symbol else (pos.market_price if pos else 0))
                qty = order.remaining_quantity
                total_value += ref * qty
                cash += ref * qty + broker._calculate_fee(OrderSide.BUY, ref, qty)
                if r.symbol == symbol:
                    symbol_qty += qty
        return symbol_qty, total_value, cash

    # ── 台账维护 ────────────────────────────────────────────────────────────
    def update(self, key: str, symbol: str, shares: int) -> None:
        self._hold.setdefault(symbol, {})[key] = int(shares)

    def total(self, symbol: str) -> int:
        held = dict(self._hold.get(symbol, {}))
        for key, broker in self._brokers.items():
            p = broker.account.positions.get(symbol)
            held[key] = p.quantity if p else 0
        return sum(held.values())

    def others(self, symbol: str, key: str) -> int:
        broker = self._brokers.get(key)
        pos = broker.account.positions.get(symbol) if broker else None
        own = (pos.quantity if pos else 0) if broker else int(self._hold.get(symbol, {}).get(key, 0))
        return self.total(symbol) - own

    # ── 判定 ────────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self.cap_pct is not None and self.base_cash > 0

    def cap_value(self) -> float | None:
        if not self.enabled:
            return None
        return float(self.cap_pct) * self.base_cash      # type: ignore[arg-type]

    def allowed_buy(self, symbol: str, price: float, key: str,
                    own_shares: int, exclude_id: str = "") -> float | None:
        """还能买多少股；`None` = 闸门未启用，不限制。可能为负（已超限）。"""
        cap = self.cap_value()
        if cap is None or price <= 0:
            return None
        pending, _, _ = self._pending(symbol, price, key, exclude_id)
        room = cap - (self.others(symbol, key) + int(own_shares) + pending) * float(price)
        return room / float(price)

    def check(self, symbol: str, qty: int, price: float, key: str,
              own_shares: int, strategy_id: str = "", at: str = "", exclude_id: str = "") -> bool:
        """是否放行这笔买入（未启用闸门恒为 True）。"""
        allowed = self.allowed_buy(symbol, price, key, own_shares, exclude_id)
        reason = "跨策略单标上限"
        ok = allowed is None or qty <= allowed
        if ok and self.enabled and self._brokers:
            _, pending_value, frozen = self._pending(symbol, price, key, exclude_id)
            cash = self.base_cash + sum(b.account.cash - b.account.initial_cash for b in self._brokers.values())
            value = sum(p.quantity * (price if s == symbol else p.market_price)
                        for b in self._brokers.values() for s, p in b.account.positions.items())
            equity = cash + value
            broker = self._brokers.get(key)
            fee = broker._calculate_fee(OrderSide.BUY, price, qty) if broker else 0
            if price * qty + fee > cash - frozen:
                ok, reason = False, "跨策略总资金不足(含挂单)"
            elif value + pending_value + price * qty > equity * self.max_account_position_pct:
                ok, reason = False, "跨策略总仓位上限(含挂单)"
            elif sum(b.account.daily_realized_pnl for b in self._brokers.values()
                     if broker is None or b.account.trading_day == broker.account.trading_day
                     ) <= -self.base_cash * self.max_daily_loss_pct:
                ok, reason = False, "跨策略日亏只减仓"
        if ok:
            return True
        self.rejects.append({
            "symbol": symbol, "strategy_id": strategy_id, "at": at,
            "qty": int(qty), "price": float(price),
            "own_shares": int(own_shares),
            "other_shares": self.others(symbol, key),
            "cap_value": self.cap_value(),
            "reason": reason,
        })
        return False


class PortfolioGate:
    """准入、跳空/续成均检查；当前订单从挂单统计排除，避免重复占额。"""

    def __init__(self, ledger, key, broker, strategy_id=""):
        self.ledger, self.key, self.broker, self.strategy_id = ledger, key, broker, strategy_id
        ledger.bind(key, broker)

    def _check(self, symbol, quantity, price, at, exclude_id=""):
        pos = self.broker.account.positions.get(symbol)
        slipped = price * (1 + self.broker.config.slippage_bps / 10000.0)
        ok = self.ledger.check(symbol, quantity, slipped, self.key,
                               pos.quantity if pos else 0, self.strategy_id, str(at), exclude_id)
        if not ok:
            self.last_rejection = self.ledger.rejects[-1]["reason"]
        return ok

    def other_volume_used(self, symbol, ts):
        stamp = ts.isoformat()
        used = 0
        for key, broker in self.ledger._brokers.items():
            usage = broker.volume_budget.get(symbol, {})
            if key != self.key and usage.get("time") == stamp:
                used += usage["used"]
        return used

    def __call__(self, signal, price, own_shares):
        return self._check(signal.symbol, signal.quantity, price, signal.signal_time)

    def check_order(self, order, price):
        return self._check(order.request.symbol, order.remaining_quantity, price,
                           order.request.created_at, order.request.client_order_id)
