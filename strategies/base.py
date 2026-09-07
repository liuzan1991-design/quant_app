# -*- coding: utf-8 -*-
"""策略统一接口：新增策略只需实现 BaseStrategy 并注册。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd


@dataclass
class ParamMeta:
    """侧边栏控件元数据，界面据此自动生成参数输入控件。"""
    key: str
    label: str
    default: Any
    kind: str = "number"      # number / slider / checkbox / select / text
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: Optional[List] = None
    help: str = ""
    group: str = "core"       # core=核心 / risk=风控 / cost=交易成本


@dataclass
class BacktestResult:
    """统一回测结果，界面/图表/导出只认这个结构。"""
    strategy_id: str
    strategy_name: str
    params: Dict[str, Any]
    init_cash: float
    equity: pd.DataFrame                    # time, equity, close, cash, position
    trades: pd.DataFrame                    # time, direction, price, shares, reason, score, fee, t_pnl
    rejected: pd.DataFrame = field(default_factory=lambda: pd.DataFrame())  # P1：被拒委托（引擎暂未产出）
    total_fees: float = 0.0
    final_equity: float = 0.0
    total_return: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    calmar: float = 0.0
    t_sell_count: int = 0
    t_win_rate: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    trade_days: int = 0
    base_price: Optional[float] = None       # 底仓建仓价（用于基准对比）
    base_shares: int = 0
    avg_exposure: float = 0.0                # 平均持仓市值 / 账户权益
    max_exposure: float = 0.0                # 最大持仓市值 / 账户权益
    capital_return: float = 0.0              # 净利润 / 平均占用资金
    data_adjustment: str = "不复权"


class BaseStrategy:
    id: str = "base"
    name: str = "基础策略"
    description: str = ""
    param_meta: List[ParamMeta] = []

    def run(self, df: pd.DataFrame, init_cash: float, params: Dict[str, Any],
            context: Optional[Dict[str, Any]] = None) -> BacktestResult:
        """执行回测，返回统一结果结构。"""
        raise NotImplementedError

    @classmethod
    def default_params(cls) -> Dict[str, Any]:
        return {m.key: m.default for m in cls.param_meta}
