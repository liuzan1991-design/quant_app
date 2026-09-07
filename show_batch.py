# -*- coding: utf-8 -*-
"""展示批量回测结果（含股票涨跌与超额收益）。"""
import pandas as pd

df = pd.read_csv(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\reports\batch_2023_5sectors.csv",
                 encoding="utf-8-sig")
for sector, g in df.groupby("板块"):
    print(f"===== {sector} =====")
    for _, r in g.iterrows():
        exc = r["总收益%"] - r["股票涨跌%"]
        print(f"{r['股票']:14s} 股票涨跌 {r['股票涨跌%']:+6.2f}%  |  "
              f"{r['策略']:10s} {r['总收益%']:+6.2f}% (超额{exc:+6.2f}%)  |  "
              f"回撤 {r['最大回撤%']:6.2f}%  |  {r['交易笔数']:3d}笔")
    print()
