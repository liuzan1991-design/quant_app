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


def freeze_params(strategy_id: str, first_price: float, base_params: dict,
                  init_cash: float | None = None,
                  risk_pct: float | None = None) -> dict:
    """按观察期启动价冻结参数。

    - 网格（grid_trade）：base_position / trade_shares / max_position 按 `risk_pct` 现价算。
    - 均线波段（ma_swing）：position_pct = risk_pct×100。
    - 其它策略：原样返回。

    `init_cash` / `risk_pct` 显式传入时以调用方为准（H64：实盘口径来自 live_config），
    不传则用模块常量 ⇒ **无配置时行为与旧版一致**。
    """
    cash = float(init_cash) if init_cash else INIT_CASH
    pct = float(risk_pct) if risk_pct else RISK_PCT
    params = dict(base_params)
    if strategy_id == "grid_trade":
        budget = cash * pct
        base = int(budget / first_price) // 100 * 100
        if base < 100:
            raise ValueError(f"{pct:.0%}仓位不足100股：price={first_price} cash={cash:,.0f}")
        trade = max(100, int(base * 0.1) // 100 * 100)
        max_pos = max(base * 2, base + 100)
        params["base_position"] = base
        params["trade_shares"] = trade
        params["max_position"] = max_pos
    elif strategy_id == "ma_swing":
        params["position_pct"] = round(pct * 100, 2)
    return params