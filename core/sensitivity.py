# -*- coding: utf-8 -*-
"""参数敏感性：买卖阈值 ±1 扫描 3×3 组，看收益/回撤如何变化。"""
from __future__ import annotations

import pandas as pd

from strategies import get_strategy


def run_sensitivity(df, init_cash, sid, params, step: int = 1) -> dict:
    sv = int(round(params.get("sell_threshold", 7)))
    bv = int(round(params.get("buy_threshold", 6)))
    strategy = get_strategy(sid)
    rows = []
    for s in (sv - step, sv, sv + step):
        for b in (bv - step, bv, bv + step):
            if s < 0 or b < 0:
                continue
            p = dict(params)
            p["sell_threshold"] = s
            p["buy_threshold"] = b
            r = strategy.run(df, init_cash, p)
            rows.append({
                "卖出阈值": s,
                "买入阈值": b,
                "总收益率%": round(r.total_return * 100, 2),
                "最大回撤%": round(r.max_drawdown * 100, 2),
                "卡玛": round(r.calmar, 2) if r.calmar == r.calmar else "n/a",
                "T胜率%": round(r.t_win_rate, 1) if r.t_sell_count else "n/a",
                "交易次数": r.buy_count + r.sell_count,
            })
    table = pd.DataFrame(rows)
    pivot = table.pivot(index="卖出阈值", columns="买入阈值", values="总收益率%")
    pivot = pivot.sort_index(ascending=False)
    return {"table": table, "pivot": pivot}
