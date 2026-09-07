# -*- coding: utf-8 -*-
"""验证趋势自适应模式：大牛股(hold防卖飞)、熊股(defense禁买)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.data import load_data  # noqa: E402
from strategies import get_strategy  # noqa: E402


def main():
    for code, name in (("300308", "中际旭创(+318%大牛)"),
                       ("688256", "寒武纪(+147%大牛)"),
                       ("002594", "比亚迪(-22%熊)"),
                       ("002371", "北方华创(+9%温和)")):
        df, _ = load_data(code, "2023-01-01", "2024-01-01")
        if not len(df):
            print(name, "无数据"); continue
        print(f"\n===== {name}（{code}） =====")
        s = get_strategy("sentiment_t")
        base = s.default_params()
        # 升级版（趋势自适应开）
        r_on = s.run(df, 1_000_000.0, base, {"code": code})
        # 关闭趋势自适应（对比）
        p_off = dict(base); p_off["trend_mode"] = False
        r_off = s.run(df, 1_000_000.0, p_off, {"code": code})
        print(f"  趋势自适应开: 收益 {r_on.total_return*100:+6.2f}% | "
              f"回撤 {r_on.max_drawdown*100:6.2f}% | 交易 {len(r_on.trades):4d}笔")
        print(f"  趋势自适应关: 收益 {r_off.total_return*100:+6.2f}% | "
              f"回撤 {r_off.max_drawdown*100:6.2f}% | 交易 {len(r_off.trades):4d}笔")


if __name__ == "__main__":
    main()
