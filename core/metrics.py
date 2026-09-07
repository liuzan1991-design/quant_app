# -*- coding: utf-8 -*-
"""P0 指标：持股不动基准、每日T收益、T盈亏分布、收益归因。口径见需求文档 3.6。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import re
from collections import Counter

from strategies.base import BacktestResult


def buy_and_hold_equity(result: BacktestResult, df: pd.DataFrame) -> pd.Series:
    """持股不动基准：期初以建仓价买入底仓股数，持有至期末；剩余现金不动、无费用。"""
    if result.base_price is None or result.base_shares <= 0:
        return pd.Series(dtype=float)
    base_cost = result.base_price * result.base_shares
    idle_cash = result.init_cash - base_cost
    if idle_cash < 0:
        idle_cash = 0.0
    closes = df.set_index("time")["close"]
    return (idle_cash + result.base_shares * closes).reindex(result.equity["time"]).ffill()


def daily_t_summary(result: BacktestResult) -> pd.DataFrame:
    """每日 T 净收益：当日卖出 T 已实现盈亏(毛) - 当日成交手续费分摊。"""
    trades = result.trades
    if not len(trades):
        return pd.DataFrame(columns=["日期", "T卖出笔数", "T毛盈亏", "手续费", "T净收益", "累计T净收益"])
    t = trades.copy()
    t["date"] = pd.to_datetime(t["time"]).dt.date
    t["t_pnl"] = pd.to_numeric(t["t_pnl"], errors="coerce")
    t["fee"] = pd.to_numeric(t["fee"], errors="coerce").fillna(0.0)
    t_pnl = t.groupby("date")["t_pnl"].sum()
    t_cnt = t[(t["direction"] == "SELL") & t["t_pnl"].notna()].groupby("date").size()
    fees = t.groupby("date")["fee"].sum()
    out = pd.DataFrame({
        "T卖出笔数": t_cnt,
        "T毛盈亏": t_pnl,
        "手续费": fees,
        "T净收益": t_pnl.fillna(0.0) - fees,
    }).fillna(0.0)
    out["累计T净收益"] = out["T净收益"].cumsum()
    out = out.reset_index().rename(columns={"date": "日期"})
    return out


def t_pnl_series(result: BacktestResult) -> pd.Series:
    """单次 T 已实现盈亏序列（毛值，用于分布直方图与盈亏比）。"""
    t = result.trades
    if not len(t):
        return pd.Series(dtype=float)
    s = pd.to_numeric(t[t["direction"] == "SELL"]["t_pnl"], errors="coerce").dropna()
    return s


def attribution(result: BacktestResult, df: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """收益归因：总收益 ≈ 底仓收益 + 累计T净收益 − 其他费用。"""
    base_pnl = 0.0
    if result.base_price is not None:
        base_pnl = (df["close"].iloc[-1] - result.base_price) * result.base_shares
    total_t_net = float(daily["T净收益"].sum()) if len(daily) else 0.0
    other_fees = float(result.total_fees) - (float(daily["手续费"].sum()) if len(daily) else 0.0)
    net_pnl = float(result.final_equity - result.init_cash)
    return {
        "底仓收益": base_pnl,
        "累计T净收益": total_t_net,
        "其他费用": -other_fees,
        "合计(≈)": base_pnl + total_t_net - other_fees,
        "实际净值变化": net_pnl,
    }


def profit_factor(result: BacktestResult) -> float:
    s = t_pnl_series(result)
    if not len(s):
        return 0.0
    wins = s[s > 0].sum()
    losses = -s[s < 0].sum()
    return float(wins / losses) if losses > 0 else float("inf")


def monthly_returns(result: BacktestResult) -> pd.DataFrame:
    """按月收益矩阵（年 × 月），用于热力图。"""
    eq = result.equity.copy()
    eq["ym"] = pd.to_datetime(eq["time"]).dt.to_period("M")
    last = eq.groupby("ym")["equity"].last()
    ret = last.pct_change().fillna(0.0) * 100
    df = pd.DataFrame({"ym": ret.index.astype(str), "ret": ret.values})
    df["year"] = df["ym"].str[:4]
    df["month"] = df["ym"].str[5:7].astype(int)
    matrix = df.pivot(index="year", columns="month", values="ret")
    matrix = matrix.reindex(columns=range(1, 13))
    return matrix


def detect_suspect_adj_dates(df: pd.DataFrame, threshold: float = -0.08) -> list:
    """疑似除权/除息日：不复权日线收盘价相对前日跳空下跌超阈值（提示用，非精确）。"""
    d = df.copy()
    d["date"] = pd.to_datetime(d["time"]).dt.date
    daily = d.groupby("date")["close"].last()
    chg = daily.pct_change()
    return [idx for idx, v in chg.items() if v < threshold]


def stock_return(df: pd.DataFrame) -> float:
    """股票区间涨跌幅（纯价格口径）：区间首根收盘价 → 末根收盘价。"""
    if not len(df):
        return 0.0
    return (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100.0


def capital_efficiency(result: BacktestResult) -> dict:
    """统一资金口径：平均/最大风险敞口与按平均占用资金计算的收益。"""
    return {
        "平均资金占用%": float(result.avg_exposure * 100),
        "最大风险敞口%": float(result.max_exposure * 100),
        "占用资金收益%": float(result.capital_return * 100),
    }


def build_summary_text(result: BacktestResult, df: pd.DataFrame, stock: str,
                       code: str, start: str, end: str) -> str:
    """生成文字版回测总结，便于复制/存档/发送。"""
    daily = daily_t_summary(result)
    attr = attribution(result, df, daily)
    bh = buy_and_hold_equity(result, df)
    bh_ret = float(bh.iloc[-1] / result.init_cash - 1) * 100 if len(bh) else 0.0
    strategy_ret = result.total_return * 100
    excess = strategy_ret - bh_ret
    pf = profit_factor(result)
    st = advanced_stats(result, df)
    cap = capital_efficiency(result)
    adj = detect_suspect_adj_dates(df)

    verdict = "做T创造正超额收益，策略优于持股不动" if excess > 0.05 \
        else ("做T收益与持股不动基本相当" if excess > -0.05 else "做T拖累收益，当前参数不如持股不动")
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
    fee_per_trade = result.total_fees / len(result.trades) if len(result.trades) else 0.0
    mtd_avg_fee = f"{fee_per_trade:.2f} 元"

    sig_str = "、".join(f"{name}×{cnt}" for name, cnt in st["top_signals"]) if st["top_signals"] else "无"
    best_m = f"{st['best_month'][0]} {st['best_month'][1]:+.2f}%" if st["best_month"] else "n/a"
    worst_m = f"{st['worst_month'][0]} {st['worst_month'][1]:+.2f}%" if st["worst_month"] else "n/a"
    dd_end = f"{st['dd_end']:%Y-%m-%d}" if st["dd_end"] is not None else "未恢复"
    slippage_line = (f"滑点成本估算约 {st['slippage_cost']:,.0f} 元"
                     f"（滑点 {result.params.get('slippage_bps', 0):.0f} bps）"
                     if st["slippage_cost"] > 0 else
                     "未设滑点（可到交易成本中设置 bps 更贴近实际）")

    lines = [
        f"回测总结：{stock}（{code}）",
        f"区间：{start} ~ {end}（{result.trade_days} 个交易日）｜策略：{result.strategy_name}",
        f"初始资金：{result.init_cash:,.0f} 元",
        "",
        "【核心指标】",
        f"总收益率 {strategy_ret:+.2f}% ｜ 年化 {result.annual_return*100:+.2f}% ｜ "
        f"最大回撤 {result.max_drawdown*100:.2f}%",
        f"卡玛比率 {result.calmar:.2f} ｜ 交易次数 {result.buy_count+result.sell_count} ｜ "
        f"T胜率 {result.t_win_rate:.1f}%",
        f"平均资金占用 {cap['平均资金占用%']:.1f}% ｜ 最大风险敞口 {cap['最大风险敞口%']:.1f}% ｜ "
        f"占用资金收益 {cap['占用资金收益%']:+.2f}%",
        "",
        "【收益归因】",
        f"底仓收益 {attr['底仓收益']:+,.2f} 元 ｜ 累计T净收益 {attr['累计T净收益']:+,.2f} 元",
        f"累计手续费 {result.total_fees:,.2f} 元 ｜ 实际净值变化 {attr['实际净值变化']:+,.2f} 元",
        "",
        "【基准对比】",
        f"持股不动收益 {bh_ret:+.2f}% ｜ 策略超额 {excess:+.2f} 个百分点",
        f"结论：{verdict}",
        "",
        "【T交易质量】",
        f"T卖出 {st['t_count']} 笔，胜率 {result.t_win_rate:.1f}%，盈亏比 {pf_str}",
        f"单笔期望 {st['ev']:+.2f} 元（胜率×平均盈利 − 败率×平均亏损）",
        f"平均盈利 {st['avg_win']:+.2f} 元 ｜ 平均亏损 {st['avg_loss']:+.2f} 元",
        f"最大单笔盈利 {st['max_win']:+.2f} 元 ｜ 最大单笔亏损 {st['max_loss']:+.2f} 元",
        f"最长连续盈利 {st['max_win_streak']} 笔 ｜ 最长连续亏损 {st['max_loss_streak']} 笔",
        f"日均 T 净收益 {st['daily_t_avg']:+.2f} 元",
        "",
        "【成本分析】",
        f"累计手续费 {result.total_fees:,.2f} 元（占净盈亏 {st['fee_ratio']:.1f}%）",
        f"每笔平均费用 {mtd_avg_fee} ｜ 区间成交额 {st['turnover']:,.0f} 元",
        slippage_line,
        "",
        "【交易节奏与信号】",
        f"日均交易 {st['trades_per_day']:.2f} 笔（含建仓）",
        f"触发信号 TOP5：{sig_str}",
        "",
        "【分月表现】",
        f"最好月份 {best_m} ｜ 最差月份 {worst_m}",
        f"赚钱月份 {st['profit_months']}/{st['total_months']} 个",
        "",
        "【回撤详情】",
        f"最大回撤 {result.max_drawdown*100:.2f}%（{st['dd_trough']:%Y-%m-%d} 触底）",
        f"回撤区间 {st['dd_start']:%Y-%m-%d} ~ {dd_end}，持续 {st['dd_days']} 天",
        "",
        "【备注】",
        f"数据口径：{result.data_adjustment}；",
        f"疑似除权/除息日：{'、'.join(str(d) for d in adj[:5]) if adj else '无'}",
    ]
    return "\n".join(lines)


def advanced_stats(result: BacktestResult, df: pd.DataFrame) -> dict:
    """深度分析统计：T质量、成本、节奏、信号、月度、回撤。"""
    s = t_pnl_series(result)
    wins = s[s > 0]
    losses = s[s < 0]
    n = len(s)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    ev = (len(wins) / n * avg_win + len(losses) / n * avg_loss) if n else 0.0

    def max_streak(series, positive: bool):
        best = cur = 0
        for v in series:
            hit = (v > 0) if positive else (v < 0)
            cur = cur + 1 if hit else 0
            best = max(best, cur)
        return best

    # 信号统计（从交易原因中提取）
    sig = Counter()
    for reason in result.trades["reason"].astype(str):
        for m in re.findall(r"([^\[\]()]+)\(x\d+\)", reason):
            name = m.strip().strip("+").strip()
            if name:
                sig[name] += 1
    top_signals = sig.most_common(5)

    # 月度表现（排除首月，因 pct_change 起点为 0）
    mtx = monthly_returns(result)
    flat = []
    for y in mtx.index:
        for mth in mtx.columns:
            v = mtx.loc[y, mth]
            if pd.notna(v) and abs(v) > 1e-9:
                flat.append((f"{y}-{mth:02d}", float(v)))
    best_m = max(flat, key=lambda x: x[1]) if flat else None
    worst_m = min(flat, key=lambda x: x[1]) if flat else None
    profit_months = sum(1 for _, v in flat if v > 0)

    # 回撤详情
    eq = result.equity.reset_index(drop=True)
    peak = eq["equity"].cummax()
    dd = (eq["equity"] - peak) / peak
    trough_i = int(dd.idxmin())
    start_i = 0
    for i in range(trough_i, -1, -1):
        if eq["equity"].iloc[i] == peak.iloc[i]:
            start_i = i
            break
    peak_val = peak.iloc[trough_i]
    end_i = None
    for i in range(trough_i, len(eq)):
        if eq["equity"].iloc[i] >= peak_val:
            end_i = i
            break
    dates = pd.to_datetime(eq["time"]).dt.date
    dd_days = dates.iloc[end_i] - dates.iloc[start_i] if end_i is not None \
        else dates.iloc[-1] - dates.iloc[start_i]

    # 成交额与滑点成本估算
    trades = result.trades
    turnover = float((trades["price"].astype(float) * trades["shares"].astype(float)).sum()) if len(trades) else 0.0
    slippage = float(result.params.get("slippage_bps", 0.0))
    slippage_cost = turnover * slippage / 10000.0 if slippage > 0 else 0.0

    daily = daily_t_summary(result)
    net_pnl = abs(float(result.final_equity - result.init_cash))
    fee_ratio = result.total_fees / net_pnl * 100 if net_pnl > 1 else 0.0

    return {
        "t_count": n,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "ev": ev,
        "max_win": float(s.max()) if n else 0.0,
        "max_loss": float(s.min()) if n else 0.0,
        "max_win_streak": max_streak(s, True),
        "max_loss_streak": max_streak(s, False),
        "top_signals": top_signals,
        "best_month": best_m,
        "worst_month": worst_m,
        "profit_months": profit_months,
        "total_months": len(flat),
        "dd_trough": pd.Timestamp(eq["time"].iloc[trough_i]),
        "dd_start": pd.Timestamp(eq["time"].iloc[start_i]),
        "dd_end": pd.Timestamp(eq["time"].iloc[end_i]) if end_i is not None else None,
        "dd_days": dd_days.days,
        "turnover": turnover,
        "slippage_cost": slippage_cost,
        "fee_ratio": fee_ratio,
        "daily_t_avg": float(daily["T净收益"].mean()) if len(daily) else 0.0,
        "trades_per_day": (len(trades) - 1) / result.trade_days if result.trade_days else 0.0,
    }
