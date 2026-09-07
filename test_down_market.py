# -*- coding: utf-8 -*-
"""下跌行情压力测试：2024年初微盘股崩盘段，三策略抗跌能力实测。"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import data_fetch  # noqa: E402
from core.data import load_data  # noqa: E402
from strategies import get_strategy  # noqa: E402

START = "2024-01-02"
END = "2024-02-08"

STOCKS = [
    ("000572", "海马汽车", "小盘"),
    ("300364", "中文在线", "中盘"),
    ("601619", "嘉泽新能", "中小盘"),
]


def main():
    # 中证2000 指数确认暴跌段
    try:
        idx = data_fetch.fetch_kline("000852.SH", START, END)
        if len(idx):
            chg = (idx["close"].iloc[-1] / idx["close"].iloc[0] - 1) * 100
            print(f"中证2000指数（小盘股代表）{START}~{END}: {chg:+.1f}%")
    except Exception as e:
        print("指数拉取失败:", str(e)[:100])

    for code, name, tag in STOCKS:
        try:
            data_fetch.incremental_update(code, START, END)
            df, _ = load_data(code, START, END)
        except Exception as e:
            print(f"{name} 数据失败: {e}")
            continue
        if not len(df):
            print(f"{name} 无数据")
            continue
        sret = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
        print(f"\n===== {name}（{code}，{tag}）股票本身 {sret:+.1f}% =====")
        for sid in ("intraday_t", "grid_trade", "sentiment_t"):
            s = get_strategy(sid)
            r = s.run(df, 1_000_000.0, s.default_params(), {"code": code})
            print(f"  {s.name:10s} 收益 {r.total_return*100:+6.2f}% (超额{r.total_return*100-sret:+6.2f}%) | "
                  f"回撤 {r.max_drawdown*100:6.2f}% | 交易 {len(r.trades):4d}笔")


if __name__ == "__main__":
    main()
