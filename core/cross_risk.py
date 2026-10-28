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


class CrossSymbolLedger:
    """跨策略同标的持仓台账 + 买入额度判定。"""

    def __init__(self, cap_pct: float | None, base_cash: float) -> None:
        self.cap_pct = None if cap_pct is None else float(cap_pct)
        self.base_cash = float(base_cash)
        self._hold: dict[str, dict[str, int]] = {}
        self.rejects: list[dict] = []

    # ── 台账维护 ────────────────────────────────────────────────────────────
    def update(self, key: str, symbol: str, shares: int) -> None:
        self._hold.setdefault(symbol, {})[key] = int(shares)

    def total(self, symbol: str) -> int:
        return sum(self._hold.get(symbol, {}).values())

    def others(self, symbol: str, key: str) -> int:
        return self.total(symbol) - int(self._hold.get(symbol, {}).get(key, 0))

    # ── 判定 ────────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self.cap_pct is not None and self.base_cash > 0

    def cap_value(self) -> float | None:
        if not self.enabled:
            return None
        return float(self.cap_pct) * self.base_cash      # type: ignore[arg-type]

    def allowed_buy(self, symbol: str, price: float, key: str,
                    own_shares: int) -> float | None:
        """还能买多少股；`None` = 闸门未启用，不限制。可能为负（已超限）。"""
        cap = self.cap_value()
        if cap is None or price <= 0:
            return None
        room = cap - (self.others(symbol, key) + int(own_shares)) * float(price)
        return room / float(price)

    def check(self, symbol: str, qty: int, price: float, key: str,
              own_shares: int, strategy_id: str = "", at: str = "") -> bool:
        """是否放行这笔买入（未启用闸门恒为 True）。"""
        allowed = self.allowed_buy(symbol, price, key, own_shares)
        if allowed is None or qty <= allowed:
            return True
        self.rejects.append({
            "symbol": symbol, "strategy_id": strategy_id, "at": at,
            "qty": int(qty), "price": float(price),
            "own_shares": int(own_shares),
            "other_shares": self.others(symbol, key),
            "cap_value": self.cap_value(),
            "reason": "跨策略单标上限",
        })
        return False
