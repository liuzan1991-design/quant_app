# -*- coding: utf-8 -*-
"""网格策略冒烟测试：跑通 + 与做T策略对比。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.data import load_data  # noqa: E402
from strategies import get_strategy  # noqa: E402


def main():
    for code in ("601619", "688256"):
        df, _ = load_data(code)
        if not len(df):
            print(code, "无数据，跳过")
            continue
        print(f"\n===== {code}（{len(df):,} 根K线） =====")
        for sid in ("grid_trade", "intraday_t"):
            s = get_strategy(sid)
            r = s.run(df, 1_000_000.0, s.default_params())
            print(f"  {r.strategy_name:8s} 收益 {r.total_return*100:+.2f}% | "
                  f"回撤 {r.max_drawdown*100:.2f}% | 交易 {len(r.trades)} 笔 "
                  f"(买{r.buy_count}/卖{r.sell_count}) | T胜率 {r.t_win_rate:.1f}% | "
                  f"手续费 {r.total_fees:,.1f}")
        for sid in ("sentiment_t",):
            s = get_strategy(sid)
            r = s.run(df, 1_000_000.0, s.default_params(), {"code": code})
            print(f"  {r.strategy_name:8s} 收益 {r.total_return*100:+.2f}% | "
                  f"回撤 {r.max_drawdown*100:.2f}% | 交易 {len(r.trades)} 笔 "
                  f"(买{r.buy_count}/卖{r.sell_count}) | T胜率 {r.t_win_rate:.1f}% | "
                  f"手续费 {r.total_fees:,.1f}")
        g = get_strategy("grid_trade").run(df, 1_000_000.0, get_strategy("grid_trade").default_params())
        if len(g.rejected):
            print(f"  网格被拒记录 {len(g.rejected)} 条，示例：{g.rejected['reason'].iloc[0]}")
    print("\n网格策略冒烟测试完成")


if __name__ == "__main__":
    main()
