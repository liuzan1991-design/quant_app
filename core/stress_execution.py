# -*- coding: utf-8 -*-
"""生产同一回放核心上的扰动；未到期订单不回填，等待状态随账户持久化。"""
from __future__ import annotations

import random

from core.paper_replay import OfflineReplayEngine, SignalOrderAdapter


class StressExecutionEngine(OfflineReplayEngine):
    def __init__(self, broker, store=None, *, drop_rate=0.0, latency_bars=0,
                 apply_limit=False, seed=42, gate=None):
        super().__init__(broker, store, gate=gate)
        if not 0 <= drop_rate <= 1 or latency_bars < 0:
            raise ValueError("丢单率须在[0,1]，延迟不得为负")
        self.drop_rate = drop_rate
        self.latency_bars = max(1, int(latency_bars))
        self.apply_limit = apply_limit
        self.seed = seed

    def _drop_signal(self, signal):
        order_id = SignalOrderAdapter.to_order(signal).client_order_id
        return random.Random(f"{self.seed}:{order_id}").random() < self.drop_rate

    def _prepare_bar(self, bar, symbol):
        if not self.apply_limit:
            return bar
        day = bar["time"].date().isoformat()
        state = self.broker.replay_market_context.setdefault(symbol, {"day": day, "last": None, "prev": None})
        if day != state["day"]:
            state.update(day=day, prev=state["last"])
        out = bar.copy()
        if state["prev"] is not None:
            pct = .20 if symbol.startswith(("30", "68")) else .30 if symbol.endswith(".BJ") else .10
            out["limit_up"] = round(state["prev"] * (1 + pct), 2)
            out["limit_down"] = round(state["prev"] * (1 - pct), 2)
        state["last"] = float(bar["close"])
        return out
