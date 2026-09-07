# -*- coding: utf-8 -*-
"""策略注册表：新增策略在此注册即可被 APP 自动发现。"""
from strategies.base import BacktestResult, BaseStrategy, ParamMeta
from strategies.grid_trade import GridTradeStrategy
from strategies.intraday_t import IntradayTStrategy
from strategies.ma_swing import MaSwingStrategy
from strategies.sentiment_t import SentimentTStrategy

STRATEGIES = {
    "intraday_t": IntradayTStrategy,
    "grid_trade": GridTradeStrategy,
    "sentiment_t": SentimentTStrategy,
    "ma_swing": MaSwingStrategy,
}


def get_strategy(strategy_id: str) -> BaseStrategy:
    return STRATEGIES[strategy_id]()


def list_strategies() -> list:
    return [(sid, cls.name, cls.description) for sid, cls in STRATEGIES.items()]
