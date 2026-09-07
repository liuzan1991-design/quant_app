# -*- coding: utf-8 -*-
"""聚焦重跑：未来VWAP修复后，intraday_t / sentiment_t 的新口径收益。
与 validate_strategies_long.py 使用完全相同的 30% 仓位口径，确保与旧验证明细可比。
只跑受 VWAP 修复影响的两个策略（走 run_backtest），grid 不受影响故跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from core.data import load_data, stock_name  # noqa: E402
from strategies import get_strategy  # noqa: E402

STOCKS = ["688256", "300308", "300502", "300418", "300364", "002371", "601619", "000572", "600900", "002594"]
STRATEGY_IDS = ["intraday_t", "sentiment_t"]
PERIODS = [
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026YTD", "2026-01-01", "2026-08-21"),
    ("全周期", "2023-01-01", "2026-08-21"),
]


def main() -> None:
    rows = []
    for code in STOCKS:
        for period_name, start, end in PERIODS:
            df, source = load_data(code, start, end)
            if df.empty or pd.to_datetime(df["time"]).dt.date.nunique() < 40:
                continue
            first_price = float(df["close"].iloc[0])
            for sid in STRATEGY_IDS:
                strategy = get_strategy(sid)
                try:
                    params = strategy.default_params()
                    init_cash = 1_000_000.0
                    base_shares = int((init_cash * 0.30 / first_price) // 100 * 100)
                    if base_shares < 100:
                        raise ValueError("资金不足")
                    params["base_position"] = base_shares
                    if "fixed_shares" in params:
                        params["fixed_shares"] = max(100, int(base_shares * 0.1 // 100 * 100))
                    if "trade_shares" in params:
                        params["trade_shares"] = max(100, int(base_shares * 0.1 // 100 * 100))
                    params["max_position"] = max(base_shares * 2, base_shares + 100)
                    result = strategy.run(df, init_cash, params, {"code": code})
                    rows.append({
                        "股票代码": code, "股票名称": stock_name(code), "阶段": period_name,
                        "策略ID": sid, "策略": strategy.name,
                        "策略收益%": round(result.total_return * 100, 2),
                        "策略最大回撤%": round(result.max_drawdown * 100, 2),
                        "交易笔数": len(result.trades), "手续费": round(result.total_fees, 2),
                    })
                except Exception as exc:  # noqa: BLE001
                    rows.append({"股票代码": code, "股票名称": stock_name(code),
                                 "阶段": period_name, "策略ID": sid, "策略": strategy.name,
                                 "策略收益%": None, "策略最大回撤%": None,
                                 "交易笔数": None, "手续费": None, "错误": str(exc)})
                print(f"[done] {stock_name(code)} {period_name} {strategy.name}", flush=True)
    out = pd.DataFrame(rows)
    out_path = Path(r"D:\Codex输出\VWAP修复后口径_重验明细.csv")
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    print("=== 全周期汇总 ===")
    full = out[out["阶段"] == "全周期"]
    print(full.to_string(index=False))
    print(f"\n明细已写入: {out_path}")


if __name__ == "__main__":
    main()
