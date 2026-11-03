# -*- coding: utf-8 -*-
"""移动网格共享状态机的离线回放适配层。

复用 `GridSharedEngine` 的信号与状态逻辑，把每根 bar 上的交易意图转成
`StrategySignal`，供 `OfflineReplayEngine` 交给 `PaperBroker` 撮合。
信号生成与 `test_grid_shared_consistency.py` 保持同一口径。
"""
from __future__ import annotations

import sys
from math import ceil
from pathlib import Path
from typing import Dict, Iterable, Optional

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backtest_generic as bg  # noqa: E402
from core.adjustment import prepare_signal_prices  # noqa: E402
from core.grid_shared import GridSharedEngine  # noqa: E402
from core.intraday_t_broker import BrokerTAccount, filled_inventory  # noqa: E402
from core.paper_models import OrderSide, TERMINAL_ORDER_STATUSES  # noqa: E402
from core.paper_replay import StrategySignal  # noqa: E402


class GridSharedLiveEngine:
    def __init__(self, symbol: str, params: Dict, strategy_id: str = "grid_trade",
                 df: Optional[pd.DataFrame] = None, *, broker_driven: bool = False):
        self.symbol = symbol
        self.strategy_id = strategy_id
        self.params = dict(params)
        self.params["_init_cash"] = 1_000_000.0
        self.engine = GridSharedEngine(self.params)
        self.broker_driven = broker_driven
        self.account = None if broker_driven else bg.SimAccount(1_000_000.0, self.params)
        self._code = symbol.split(".")[0]
        self._signal_close: Dict[pd.Timestamp, float] = {}
        self._prepared = False
        if df is not None:
            self.prepare(df)

    def prepare(self, df: pd.DataFrame) -> None:
        if self._prepared:
            return
        prepared, _events, _adj = prepare_signal_prices(df, self._code)
        for _, row in prepared.iterrows():
            self._signal_close[pd.Timestamp(row["time"])] = float(row["signal_close"])
        self._prepared = True

    def _sync_lifecycle_from_fills(self, broker):
        if not self.broker_driven:
            return
        base = int(self.params.get("base_position", 1000))
        if broker.corporate_action_events:
            _, _, base = filled_inventory(broker, self.symbol, base)
        self.account = BrokerTAccount(broker, self.symbol, base)
        self.engine._base_position = base
        state = self.engine.state
        extra = max(0, self.account.total - self.account.base_position)
        state.grid_bought = ceil(extra / max(1, int(self.params.get("trade_shares", 100))))
        self.engine._init_cash = broker.account.initial_cash
        if state.base_price is None and self.account.total:
            state.base_price = self.account.position.average_cost
        elif not self.account.total and base:
            state.base_price = None
        if state.anchor_price is None and self.account.total:
            state.anchor_price = self.account.position.average_cost

    def on_bar(self, bar: pd.Series, _broker) -> Iterable[StrategySignal]:
        if not self._prepared:
            raise RuntimeError("GridSharedLiveEngine.prepare(df) 未调用")
        ts = pd.Timestamp(bar["time"])
        bar = bar.copy()
        bar["signal_close"] = self._signal_close.get(ts, float(bar["close"]))
        if self.broker_driven:
            self._sync_lifecycle_from_fills(_broker)
            before_grid = self.engine.state.grid_bought
            before_base = self.engine.state.base_price
            pending = any(o.request.symbol == self.symbol and o.status not in TERMINAL_ORDER_STATUSES
                          for o in _broker.orders.values())
            # 挂单期间仍更新VWAP/跨日状态，但不再产生第二张交易单。
            self.account.trading_enabled = not pending
            out = self.engine.on_bar(bar, self.account)
            self.engine.state.grid_bought = before_grid
            if out and any(t.get("reason") == "建底仓" and not t.get("rejected") for t in out):
                self.engine.state.base_price = before_base
        else:
            out = self.engine.on_bar(bar, self.account)
        if not out:
            return ()
        signals = []
        for t in out:
            if t.get("rejected"):
                continue
            side = OrderSide.BUY if t["direction"] == "BUY" else OrderSide.SELL
            signals.append(StrategySignal(
                strategy_id=self.strategy_id,
                symbol=self.symbol,
                side=side,
                quantity=int(t["shares"]),
                signal_time=pd.Timestamp(t["time"]).to_pydatetime(),
                reason=t.get("reason", ""),
            ))
            if self.broker_driven:
                break  # 试算不动真实库存，止损/减仓意图优先，只发第一笔。
        return tuple(signals)
