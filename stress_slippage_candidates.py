# -*- coding: utf-8 -*-
"""复权后候选组合的滑点压力测试。

覆盖滑点 0/5/10/20 bps，回答：三个可观察候选在不同成交偏差下，
收益/回撤/交易笔数/手续费是否仍稳定。

只做离线快速回测，不连接行情或券商；复用现有策略入口（已接入复权口径）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from strategies import get_strategy  # noqa: E402

OUT_DIR = Path(r"D:\Codex输出\压力测试")
INIT_CASH = 1_000_000.0

CANDIDATES = [
    ("300308", "grid_trade"),
    ("300308", "ma_swing"),
    ("688256", "sentiment_t"),
]
SLIPPAGE_BPS = (0, 5, 10, 20)


def run_one(code: str, strategy_id: str, slippage_bps: float) -> dict:
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    strategy = get_strategy(strategy_id)
    params = strategy.default_params()
    if "slippage_bps" in params:
        params["slippage_bps"] = slippage_bps

    # 统一约30%初始风险敞口，使不同股价候选可比。
    if strategy_id == "ma_swing":
        params["position_pct"] = 30.0
    else:
        first_price = float(df["close"].iloc[0])
        base_shares = int((INIT_CASH * 0.30 / first_price) // 100 * 100)
        if base_shares < 100:
            raise ValueError(f"{code} 100万元不足以按统一仓位买入100股")
        params["base_position"] = base_shares
        if "fixed_shares" in params:
            params["fixed_shares"] = max(100, int(base_shares * 0.1 // 100 * 100))
        if "trade_shares" in params:
            params["trade_shares"] = max(100, int(base_shares * 0.1 // 100 * 100))
        params["max_position"] = max(base_shares * 2, base_shares + 100)

    result = strategy.run(df, INIT_CASH, params, {"code": code})
    stock_return = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1)
    return {
        "股票": code,
        "策略": strategy_id,
        "滑点bps": slippage_bps,
        "总收益%": round(result.total_return * 100, 2),
        "最大回撤%": round(result.max_drawdown * 100, 2),
        "交易笔数": len(result.trades),
        "手续费": round(result.total_fees, 2),
        "相对持股%": round(result.total_return * 100 - stock_return * 100, 2),
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for code, strategy_id in CANDIDATES:
        for bps in SLIPPAGE_BPS:
            try:
                rows.append(run_one(code, strategy_id, bps))
            except Exception as exc:  # noqa: BLE001
                rows.append({"股票": code, "策略": strategy_id, "滑点bps": bps,
                             "错误": str(exc)})

    detail = pd.DataFrame(rows)
    detail.to_csv(OUT_DIR / "候选滑点压力测试.csv", index=False, encoding="utf-8-sig")
    print(detail.to_string(index=False))

    pivot_cols = ["总收益%", "最大回撤%", "交易笔数", "手续费"]
    print("\n===== 按候选分组的滑点敏感性（收益%） =====")
    ok = detail[~detail["滑点bps"].isna()].copy()
    for (code, sid), g in ok.groupby(["股票", "策略"]):
        g = g.sort_values("滑点bps")
        line = " | ".join(f"{int(r['滑点bps'])}bps:{r['总收益%']:+.2f}%" for _, r in g.iterrows())
        print(f"{code}×{sid}: {line}")


if __name__ == "__main__":
    main()