# -*- coding: utf-8 -*-
"""移动网格共享状态机的离线回放适配层。

复用 `GridSharedEngine` 的信号与状态逻辑，把每根 bar 上的交易意图转成
`StrategySignal`，供 `OfflineReplayEngine` 交给 `PaperBroker` 撮合。
信号生成与 `test_grid_shared_consistency.py` 保持同一口径。
"""
from __future__ import annotations

import sys
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
from core.paper_models import OrderSide  # noqa: E402
from core.paper_replay import StrategySignal  # noqa: E402


class GridSharedLiveEngine:
    def __init__(self, symbol: str, params: Dict, strategy_id: str = "grid_trade",
                 df: Optional[pd.DataFrame] = None):
        self.symbol = symbol
        self.strategy_id = strategy_id
        self.params = dict(params)
        self.params["_init_cash"] = 1_000_000.0
        self.engine = GridSharedEngine(self.params)
        self.account = bg.SimAccount(1_000_000.0, self.params)
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

    def on_bar(self, bar: pd.Series, _broker) -> Iterable[StrategySignal]:
        if not self._prepared:
            raise RuntimeError("GridSharedLiveEngine.prepare(df) 未调用")
        ts = pd.Timestamp(bar["time"])
        bar = bar.copy()
        bar["signal_close"] = self._signal_close.get(ts, float(bar["close"]))
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
        return tuple(signals)