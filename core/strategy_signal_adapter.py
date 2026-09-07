# -*- coding: utf-8 -*-
"""把现有策略的历史交易意图适配为 StrategySignal，不修改原 run() 快速回测路径。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pandas as pd

from core.paper_models import OrderSide
from core.paper_replay import StrategySignal
from strategies import get_strategy


@dataclass
class PreparedSignalStream:
    strategy_id: str
    symbol: str
    signals_by_time: Dict[pd.Timestamp, List[StrategySignal]]
    backtest_result: object

    def on_bar(self, bar: pd.Series, _broker) -> Iterable[StrategySignal]:
        ts = pd.Timestamp(bar["time"])
        return self.signals_by_time.get(ts, [])


class HistoricalStrategySignalAdapter:
    """兼容适配器：用现有策略生成历史交易意图，再按时间逐根释放。

    适用于回归与执行链验证。它不会替代原快速回测；严格实时 on_bar 指标引擎后续逐策略实现。
    """

    @staticmethod
    def prepare(strategy_id: str, df: pd.DataFrame, symbol: str,
                init_cash: float, params: Optional[dict] = None) -> PreparedSignalStream:
        strategy = get_strategy(strategy_id)
        merged = strategy.default_params()
        if params:
            merged.update(params)
        result = strategy.run(df.copy(), init_cash, merged, context={"code": symbol})
        signals: Dict[pd.Timestamp, List[StrategySignal]] = {}
        for index, trade in result.trades.reset_index(drop=True).iterrows():
            side = OrderSide.BUY if str(trade["direction"]).upper() == "BUY" else OrderSide.SELL
            ts = pd.Timestamp(trade["time"])
            signal = StrategySignal(
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                quantity=int(trade["shares"]),
                signal_time=ts.to_pydatetime(),
                reason=str(trade.get("reason", "")) or f"历史适配信号{index}",
            )
            signals.setdefault(ts, []).append(signal)
        return PreparedSignalStream(
            strategy_id=strategy_id,
            symbol=symbol,
            signals_by_time=signals,
            backtest_result=result,
        )
