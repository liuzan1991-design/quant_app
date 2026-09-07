# -*- coding: utf-8 -*-
"""批量回测：多股票 × 多策略，数据自动加载/拉取，返回统一结果表。"""
from __future__ import annotations

import pandas as pd

from core import data_fetch
from core.data import load_data, stock_name
from core.metrics import stock_return
from strategies import STRATEGIES, get_strategy


def run_batch(stocks: list, strategies: list, init_cash: float,
              start: str, end: str, progress=None) -> pd.DataFrame:
    """stocks: [(代码, 板块)]；strategies: [策略id]；返回结果 DataFrame。

    progress(fraction, label)：可选进度回调，fraction 为 0~1，label 为当前步骤文字。
    """
    rows = []
    total = len(stocks) * len(strategies)
    done = 0
    n_stock = len(stocks)

    def _p(frac: float, label: str) -> None:
        if progress:
            progress(max(0.0, min(frac, 1.0)), label)

    for i, (code, sector) in enumerate(stocks):
        try:
            _p(done / max(total, 1),
               f"拉取数据 {i + 1}/{n_stock}：{stock_name(code)} {code}…")
            data_fetch.incremental_update(code, start, end)
            df, _ = load_data(code, start, end)
        except Exception as e:  # noqa: BLE001
            print(f"[批量] {code} 数据失败：{e}")
            _p(done / max(total, 1), f"拉取数据 {i + 1}/{n_stock}：{code} 失败，已跳过")
            continue
        if not len(df):
            print(f"[批量] {code} 无数据")
            continue
        sret = stock_return(df)
        for sid in strategies:
            try:
                s = get_strategy(sid)
                _p(done / max(total, 1),
                   f"回测 {done + 1}/{total}：{stock_name(code)} {code} × {s.name}…")
                r = s.run(df, init_cash, s.default_params(), {"code": code})
                rows.append({
                    "板块": sector or "",
                    "股票": f"{stock_name(code)} {code}",
                    "策略": s.name,
                    "股票涨跌%": round(sret, 2),
                    "总收益%": round(r.total_return * 100, 2),
                    "年化%": round(r.annual_return * 100, 2),
                    "最大回撤%": round(r.max_drawdown * 100, 2),
                    "超额收益%": round(r.total_return * 100 - sret, 2),
                    "交易笔数": len(r.trades),
                    "T胜率%": round(r.t_win_rate, 1) if r.t_sell_count else 0,
                    "手续费": round(r.total_fees, 0),
                    "平均资金占用%": round(r.avg_exposure * 100, 1),
                    "最大风险敞口%": round(r.max_exposure * 100, 1),
                    "占用资金收益%": round(r.capital_return * 100, 2),
                })
            except Exception as e:  # noqa: BLE001
                print(f"[批量] {code} {sid} 失败：{e}")
            done += 1
            _p(done / max(total, 1),
               f"回测 {done}/{total}：{stock_name(code)} {code} 完成")
    _p(1.0, "批量回测完成")
    return pd.DataFrame(rows)


def summarize_batch(result_df: pd.DataFrame) -> str:
    """批量回测文字总结：分析各股票×策略表现，给出适用性结论。"""
    if not len(result_df):
        return "批量回测无数据"
    if "超额收益%" not in result_df.columns:
        result_df = result_df.copy()
        result_df["超额收益%"] = result_df["总收益%"] - result_df["股票涨跌%"]
    lines = []
    n_stock = result_df["股票"].nunique()
    n_strat = result_df["策略"].nunique()
    lines.append(f"批量回测总结：{n_stock} 只股票 × {n_strat} 个策略")
    lines.append("")

    best = result_df.loc[result_df["总收益%"].idxmax()]
    worst = result_df.loc[result_df["总收益%"].idxmin()]
    best_exc = result_df.loc[result_df["超额收益%"].idxmax()]
    lines.append("【总体概况】")
    lines.append(f"最佳收益组合：{best['股票']} × {best['策略']}  {best['总收益%']:+.2f}%")
    lines.append(f"最差收益组合：{worst['股票']} × {worst['策略']}  {worst['总收益%']:+.2f}%")
    lines.append(f"最大超额组合：{best_exc['股票']} × {best_exc['策略']}  "
                 f"跑赢股票 {best_exc['超额收益%']:+.2f}%")
    lines.append("")

    lines.append("【分股票诊断】")
    for stock, g in result_df.groupby("股票"):
        sret = g["股票涨跌%"].iloc[0]
        g2 = g.sort_values("总收益%", ascending=False)
        best_s = g2.iloc[0]
        worst_s = g2.iloc[-1]
        max_exc = g["超额收益%"].max()
        line = (f"· {stock}：股票自身 {sret:+.2f}% ｜ 最佳策略「{best_s['策略']}」"
                f" {best_s['总收益%']:+.2f}%（超额{best_s['超额收益%']:+.2f}%）")
        if max_exc > 1:
            line += "，策略跑赢持股，做T/网格有正价值"
        elif sret > 20 and max_exc < -5:
            line += "，**大牛股，策略全部跑输持股，此股应持股不动而非做T**"
        elif max_exc < -1:
            line += "，策略整体跑输持股，需谨慎"
        else:
            line += "，策略与持股基本相当"
        if len(g) > 1 and worst_s["总收益%"] < best_s["总收益%"] - 3:
            line += f"；最差策略「{worst_s['策略']}」{worst_s['总收益%']:+.2f}%（差{best_s['总收益%']-worst_s['总收益%']:.1f}pct）"
        lines.append(line)
    lines.append("")

    lines.append("【分策略评价】")
    for strat, g in result_df.groupby("策略"):
        avg = g["总收益%"].mean()
        win = (g["超额收益%"] > 0).sum()
        b = g.loc[g["总收益%"].idxmax()]
        lines.append(f"· {strat}：平均 {avg:+.2f}%，{win}/{len(g)} 只跑赢持股，"
                     f"最佳在 {b['股票']}（{b['总收益%']:+.2f}%）")
    lines.append("")

    losses = result_df[result_df["总收益%"] < 0]
    dd_max = result_df.loc[result_df["最大回撤%"].idxmin()]
    lines.append("【风险提示】")
    lines.append(f"最大回撤组合：{dd_max['股票']} × {dd_max['策略']}  "
                 f"{dd_max['最大回撤%']:.2f}%")
    if len(losses):
        lines.append(f"亏损组合 {len(losses)} 个："
                     + "、".join(f"{r['股票']}×{r['策略']}({r['总收益%']:+.1f}%)"
                                 for _, r in losses.head(5).iterrows()))
    else:
        lines.append("本次批量回测无亏损组合")
    return "\n".join(lines)
