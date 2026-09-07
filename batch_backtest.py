# -*- coding: utf-8 -*-
"""批量回测：五板块 × 三策略，区间 2023-01-01 ~ 2024-01-01。
股票数据自动从星耀拉取（增量缓存），情绪策略自动拉指数/行业数据。
"""
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import data_fetch, sentiment_data  # noqa: E402
from core.data import load_data  # noqa: E402
from strategies import get_strategy  # noqa: E402

START = "2023-01-01"
END = "2024-01-01"

STOCKS = [
    ("688256", "寒武纪", "电子"), ("002371", "北方华创", "电子"),
    ("300308", "中际旭创", "通信"), ("300502", "新易盛", "通信"),
    ("300418", "昆仑万维", "传媒"), ("300364", "中文在线", "传媒"),
    ("601619", "嘉泽新能", "公用事业"), ("600900", "长江电力", "公用事业"),
    ("000572", "海马汽车", "汽车"), ("002594", "比亚迪", "汽车"),
]

STRATEGIES = ["intraday_t", "grid_trade", "sentiment_t"]


def main():
    rows = []
    for code, name, sector in STOCKS:
        t0 = time.time()
        print(f"\n[{sector}] {name}（{code}）拉取数据…")
        try:
            data_fetch.incremental_update(code, START, END)
            df, _ = load_data(code, START, END)
        except Exception as e:
            print(f"  !! 数据失败：{e}")
            continue
        if not len(df):
            print("  !! 无数据")
            continue
        print(f"  数据 {len(df):,} 根，{pd.to_datetime(df['time']).dt.date.nunique()} 个交易日，"
              f"耗时 {time.time()-t0:.0f}s")
        stock_chg = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100

        for sid in STRATEGIES:
            try:
                s = get_strategy(sid)
                r = s.run(df, 1_000_000.0, s.default_params(), {"code": code})
                rows.append({
                    "板块": sector, "股票": f"{name} {code}",
                    "策略": s.name,
                    "股票涨跌%": round(stock_chg, 2),
                    "总收益%": round(r.total_return * 100, 2),
                    "年化%": round(r.annual_return * 100, 2),
                    "最大回撤%": round(r.max_drawdown * 100, 2),
                    "交易笔数": len(r.trades),
                    "T胜率%": round(r.t_win_rate, 1) if r.t_sell_count else 0,
                    "手续费": round(r.total_fees, 0),
                })
                print(f"  {s.name:10s} 收益 {r.total_return*100:+6.2f}% | "
                      f"回撤 {r.max_drawdown*100:6.2f}% | 交易 {len(r.trades):4d} 笔")
            except Exception as e:
                print(f"  !! {sid} 失败：{e}")

    out = pd.DataFrame(rows)
    out_path = Path(__file__).resolve().parent / "reports" / "batch_2023_5sectors.csv"
    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    print("\n保存:", out_path)
    print("\n==== 汇总（按板块 × 策略） ====")
    pivot = out.pivot_table(index=["板块", "股票"], columns="策略",
                            values="总收益%", aggfunc="first")
    print(pivot.to_string())


if __name__ == "__main__":
    main()
