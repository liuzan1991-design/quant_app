# -*- coding: utf-8 -*-
"""实时观察参数冻结：单票 10% 仓位，用启动当天现价算股数。"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


INIT_CASH = 1_000_000.0
RISK_PCT = 0.10


def freeze_params(strategy_id: str, first_price: float, base_params: dict) -> dict:
    """按观察期启动价冻结参数。

    - 网格（grid_trade）：base_position / trade_shares / max_position 按 10% 现价算。
    - 均线波段（ma_swing）：position_pct = 10。
    - 其它策略：原样返回。
    """
    params = dict(base_params)
    if strategy_id == "grid_trade":
        budget = INIT_CASH * RISK_PCT
        base = int(budget / first_price) // 100 * 100
        if base < 100:
            raise ValueError(f"10%仓位不足100股：price={first_price}")
        trade = max(100, int(base * 0.1) // 100 * 100)
        max_pos = max(base * 2, base + 100)
        params["base_position"] = base
        params["trade_shares"] = trade
        params["max_position"] = max_pos
    elif strategy_id == "ma_swing":
        params["position_pct"] = 10.0
    return params