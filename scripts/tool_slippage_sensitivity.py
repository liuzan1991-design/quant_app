# -*- coding: utf-8 -*-
"""验证滑点参数对回测的影响。"""
import sys
from pathlib import Path

import pandas as pd

BASE = r"D:\Documents\ChatGPT\daily work\日内做T策略"
sys.path.insert(0, BASE)
import backtest_generic as bg  # noqa: E402


def main():
    df = pd.read_csv(BASE + r"\quant_app\data\stock_601619_1m.csv", parse_dates=["time"])
    for bps in (0, 5, 10, 20):
        acc, trades, eq = bg.run_backtest(df, 1_000_000.0, {"slippage_bps": bps})
        final = acc.equity(df["close"].iloc[-1])
        print(f"滑点 {bps:>3} bps: 收益 {final/acc.init_cash*100-100:+.2f}% | "
              f"交易 {len(trades)} 笔 | 手续费 {acc.total_fees:,.1f}")


if __name__ == "__main__":
    main()
