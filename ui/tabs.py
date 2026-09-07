# -*- coding: utf-8 -*-
"""右侧主区 4 个标签页渲染。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from core import metrics
from core.batch import summarize_batch
from core.data import stock_name
from ui import charts


def _chart_download(fig, name: str, key: str) -> None:
    """图表下方 PNG 下载按钮。"""
    try:
        png = fig.to_image(format="png", width=1400, height=700, scale=1.5)
        st.download_button(f"下载PNG（{name}）", png, file_name=f"{name}.png",
                           mime="image/png", key=key)
    except Exception as e:  # noqa: BLE001
        st.caption(f"PNG 导出暂不可用：{e}")


def _metric_row(result, stock_ret: float) -> None:
    def cls(v: float) -> str:
        return "up" if v > 0 else ("down" if v < 0 else "flat")

    excess = result.total_return * 100 - stock_ret
    cards = [
        ("总收益率", f"{result.total_return*100:+.2f}%", cls(result.total_return)),
        ("年化收益", f"{result.annual_return*100:+.2f}%", cls(result.annual_return)),
        ("最大回撤", f"{result.max_drawdown*100:.2f}%", "down"),
        ("卡玛比率", f"{result.calmar:.2f}" if np.isfinite(result.calmar) else "n/a", "flat"),
        ("股票涨跌幅", f"{stock_ret:+.2f}%", cls(stock_ret)),
        ("策略超额", f"{excess:+.2f}%", cls(excess)),
        ("交易次数", f"{result.buy_count+result.sell_count}", "flat"),
        ("T胜率", f"{result.t_win_rate:.1f}%" if result.t_sell_count else "n/a", "flat"),
        ("平均资金占用", f"{result.avg_exposure*100:.1f}%", "flat"),
        ("最大风险敞口", f"{result.max_exposure*100:.1f}%", "flat"),
        ("占用资金收益", f"{result.capital_return*100:+.2f}%", cls(result.capital_return)),
        ("数据口径", result.data_adjustment, "flat"),
    ]
    html = '<div class="metric-grid">' + "".join(
        f'<div class="metric-card"><div class="k">{k}</div>'
        f'<div class="v {c}">{v}</div></div>' for k, v, c in cards) + "</div>"
    st.markdown(html, unsafe_allow_html=True)


def render_overview(result, df: pd.DataFrame, code: str = "", start: str = "",
                    end: str = "") -> None:
    stock_ret = metrics.stock_return(df)
    daily = metrics.daily_t_summary(result)
    attr = metrics.attribution(result, df, daily)
    pf = metrics.profit_factor(result)

    # ① 指标卡行
    _metric_row(result, stock_ret)
    st.divider()

    # ② 权益曲线（对齐线框图：主图 + 图例）
    fig_eq = charts.equity_chart(result, df)
    st.plotly_chart(fig_eq, width="stretch")
    _chart_download(fig_eq, "权益曲线", "dl_eq")
    st.caption(f"总资产 {result.final_equity:,.0f} 元 | 初始 {result.init_cash:,.0f} 元 | "
               f"最新净值 {result.final_equity / result.init_cash:.3f}")

    # ③ 两列：回撤曲线 + 每日净值（对齐线框图）
    c1, c2 = st.columns([1, 1])
    with c1:
        fig_dd = charts.drawdown_chart(result)
        st.plotly_chart(fig_dd, width="stretch")
        _chart_download(fig_dd, "回撤曲线", "dl_dd")
    with c2:
        st.markdown('<div class="sub-title">每日净值（最近 20 个交易日）</div>',
                    unsafe_allow_html=True)
        eq = result.equity.copy()
        eq["日期"] = pd.to_datetime(eq["time"]).dt.date
        last = eq.groupby("日期")["equity"].last().reset_index()
        last["当日收益"] = last["equity"].pct_change()
        st.dataframe(last.tail(20).sort_values("日期", ascending=False),
                     hide_index=True, width="stretch")

    # ── 深度分析（线框图之外的分析功能保留在此）──
    st.markdown('<div class="section-title">📊 深度分析</div>', unsafe_allow_html=True)

    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown('<div class="sub-title">收益归因（总收益 ≈ 底仓收益 + T净收益 − 费用）</div>',
                    unsafe_allow_html=True)
        attr_df = pd.DataFrame([
            {"项目": "底仓收益", "金额(元)": round(attr["底仓收益"], 2)},
            {"项目": "累计T净收益", "金额(元)": round(attr["累计T净收益"], 2)},
            {"项目": "其他费用", "金额(元)": round(attr["其他费用"], 2)},
            {"项目": "合计(≈)", "金额(元)": round(attr["合计(≈)"], 2)},
            {"项目": "实际净值变化", "金额(元)": round(attr["实际净值变化"], 2)},
        ])
        st.dataframe(attr_df, hide_index=True, width="stretch")
        st.caption(f"累计手续费 {result.total_fees:,.2f} 元 | 盈亏比 {pf:.2f}" if np.isfinite(pf) else
                   f"累计手续费 {result.total_fees:,.2f} 元 | 盈亏比 n/a")
    with c2:
        st.markdown('<div class="sub-title">策略 vs 持股不动</div>', unsafe_allow_html=True)
        st.caption(f"策略期末 {result.final_equity:,.0f} 元 | "
                   f"超额收益 {attr['实际净值变化'] - attr['底仓收益']:+,.0f} 元")

    c1, c2 = st.columns([1, 1])
    with c1:
        if len(daily):
            fig_t = charts.daily_t_chart(daily)
            st.plotly_chart(fig_t, width="stretch")
            _chart_download(fig_t, "每日T收益", "dl_t")
    with c2:
        if len(metrics.t_pnl_series(result)):
            fig_h = charts.t_pnl_hist(metrics.t_pnl_series(result))
            st.plotly_chart(fig_h, width="stretch")
            _chart_download(fig_h, "T盈亏分布", "dl_hist")

    if len(daily):
        st.markdown('<div class="sub-title">每日 T 收益明细（最近 15 个交易日）</div>',
                    unsafe_allow_html=True)
        st.dataframe(daily.tail(15).sort_values("日期", ascending=False),
                     hide_index=True, width="stretch")
    else:
        st.info("本区间没有 T 交易")

    fig_m = charts.monthly_heatmap(metrics.monthly_returns(result))
    st.plotly_chart(fig_m, width="stretch")
    _chart_download(fig_m, "月度收益热力图", "dl_month")

    st.markdown('<div class="sub-title">📝 文字版总结（可复制/存档）</div>',
                unsafe_allow_html=True)
    summary = metrics.build_summary_text(result, df, stock_name(code),
                                         code, start, end)
    st.code(summary, language=None)
    st.download_button("下载总结.txt", summary.encode("utf-8"),
                       file_name=f"summary_{code}.txt", mime="text/plain")


def render_kline(df: pd.DataFrame, result) -> None:
    if not len(df):
        st.warning("无数据")
        return
    days = pd.Series(pd.to_datetime(df["time"]).dt.date.unique())
    selected = st.selectbox("选择交易日（K线按日展示）", days, index=len(days) - 1)
    mask = pd.to_datetime(df["time"]).dt.date == selected
    df_day = df[mask].reset_index(drop=True)
    if not len(df_day):
        st.warning("当日无数据")
        return

    trades_day = pd.DataFrame()
    if result is not None and len(result.trades):
        t = result.trades.copy()
        t["date"] = pd.to_datetime(t["time"]).dt.date
        trades_day = t[t["date"] == selected].reset_index(drop=True)

    code = st.session_state.get("code", "")
    adj_dates = metrics.detect_suspect_adj_dates(df)
    fig_k = charts.kline_chart(df_day, trades_day, code, adj_dates)
    st.plotly_chart(fig_k, width="stretch")
    _chart_download(fig_k, "K线分析", "dl_kline")
    if adj_dates:
        st.caption(f"疑似除权/除息日（日线跳空>8%，不复权价可能跳空）："
                   f"{'、'.join(str(d) for d in adj_dates[-5:])}")

    # MACD / RSI 副图
    try:
        sys_path = str(Path(__file__).resolve().parents[2])
        import sys
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        from backtest_generic import calc_macd, calc_rsi
        closes = df_day["close"].to_numpy(dtype=np.float64)
        dif, dea, bar = calc_macd(closes, 5, 10, 3)
        rsi = calc_rsi(closes, 14)
        st.plotly_chart(charts.macd_rsi_chart(df_day, dif, dea, bar, rsi),
                        width="stretch")
    except Exception as e:  # noqa: BLE001
        st.caption(f"技术指标副图暂不可用：{e}")


def render_trades(result) -> None:
    if result is None or not len(result.trades):
        st.info("还没有回测结果，先点击左侧【开始回测】")
        return
    trades = result.trades.copy()
    if "time" in trades.columns:
        trades["time"] = pd.to_datetime(trades["time"])
    direction = st.multiselect("方向筛选", ["BUY", "SELL"],
                               default=["BUY", "SELL"])
    view = trades[trades["direction"].isin(direction)]
    st.dataframe(view, hide_index=True, width="stretch", height=480)

    buys = trades[trades["direction"] == "BUY"]
    sells = trades[trades["direction"] == "SELL"]
    c1, c2, c3 = st.columns(3)
    c1.metric("总买入次数", len(buys))
    c2.metric("总卖出次数", len(sells))
    c3.metric("累计手续费", f"{result.total_fees:,.2f} 元")

    csv = view.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下载 CSV", csv, file_name="trades.csv", mime="text/csv")

    st.divider()
    st.markdown('<div class="sub-title">被拒委托记录（冷却 / T+1 / 现金不足等未成交）</div>',
                unsafe_allow_html=True)
    if result.rejected is not None and len(result.rejected):
        rj = result.rejected.copy()
        if "time" in rj.columns:
            rj["time"] = pd.to_datetime(rj["time"])
        c1, c2 = st.columns([1, 2])
        with c1:
            st.markdown('<div class="sub-title">按原因统计</div>', unsafe_allow_html=True)
            cnt = rj["reason"].astype(str).str.replace(r"\(\d+\)", "", regex=True).value_counts()
            st.dataframe(cnt.rename("次数").reset_index().rename(columns={"index": "原因"}),
                         hide_index=True, width="stretch")
        with c2:
            st.dataframe(rj, hide_index=True, width="stretch", height=300)
    else:
        st.caption("本区间没有被拒委托")


def render_data(code: str, df: pd.DataFrame, data_file, start, end) -> None:
    if data_file is not None:
        f = Path(data_file)
        st.markdown(f'<div class="sub-title">当前股票：{code}</div>', unsafe_allow_html=True)
        st.write(f"数据文件：`{f}`")
        st.write(f"数据根数：{len(df):,} | 区间：{pd.to_datetime(df['time']).iloc[0]} → "
                 f"{pd.to_datetime(df['time']).iloc[-1]}")
        st.write(f"交易日数量：{pd.to_datetime(df['time']).dt.date.nunique()} | "
                 f"数据口径：{df.attrs.get('adjustment', '不复权（成交价）')}")
        days = pd.to_datetime(df["time"]).dt.date.unique()
        missing = []
        if len(days) > 1:
            for d1, d2 in zip(days[:-1], days[1:]):
                gap = (pd.Timestamp(d2) - pd.Timestamp(d1)).days
                if gap > 3:  # 周末/节假日通常 ≤3
                    missing.append(f"{d1} ~ {d2}（{gap-1} 天缺口）")
        if missing:
            st.warning("疑似缺失交易日：" + "；".join(missing[:5]) +
                       ("…" if len(missing) > 5 else ""))
        else:
            st.success("交易日连续，无明显缺失")
    else:
        st.warning(f"未找到 {code} 的本地 1 分钟数据。数据源：星耀数智（待接入增量拉取）。")

    st.divider()
    st.markdown("**数据源状态**")
    cred = Path(r"D:\Codex输出\ad_credentials.json")
    if cred.exists():
        st.success("星耀数智凭证已配置（数据拉取功能待 P1 接入）")
    else:
        st.info("未配置星耀数智凭证")
    st.caption("数据来源：星耀数智（AmazingData）官方 1 分钟行情，不复权；"
               "跨除权日需注意跳空，除权日标注为 P2 功能。")


def render_batch(result_df) -> None:
    """📋 批量回测页：多股票 × 多策略结果表 + 对比图 + 下载。"""
    st.markdown('<div class="section-title">📋 批量回测（多股票 × 多策略）</div>',
                unsafe_allow_html=True)
    if result_df is None or not len(result_df):
        st.info("在左侧【批量回测（多股×多策略）】面板选择股票和策略，"
                "点击【开始批量回测】。不在预置池的代码可手动输入（逗号分隔），会自动拉数据。")
        return
    st.caption("股票涨跌幅 = 区间内价格涨幅；超额收益 = 策略收益 − 股票涨跌幅")
    st.markdown('<div class="sub-title">📝 批量分析总结</div>', unsafe_allow_html=True)
    summary = summarize_batch(result_df)
    st.code(summary, language=None)
    st.download_button("下载总结.txt", summary.encode("utf-8"),
                       file_name="batch_summary.txt", mime="text/plain")
    st.dataframe(result_df, hide_index=True, width="stretch", height=420)
    st.plotly_chart(charts.batch_compare_chart(result_df), width="stretch")
    csv = result_df.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下载批量回测 CSV", csv,
                       file_name="batch_backtest.csv", mime="text/csv")
