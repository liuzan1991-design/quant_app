# -*- coding: utf-8 -*-
"""Plotly 图表：深色主题、A股红涨绿跌。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from core.metrics import buy_and_hold_equity
from strategies.base import BacktestResult

COLOR_UP = "#FF5252"      # 红=涨/买入（两主题通用）
COLOR_DOWN = "#26A69A"    # 绿=跌/卖出
COLOR_ACCENT = "#00B4D8"  # 青蓝
COLOR_YELLOW = "#FFC107"  # 尾盘平仓

# 中性色随主题切换，保证图表与页面背景一致（浅色模式不再出现深色图表）
THEME_COLORS = {
    "dark": {
        "BG_PAGE": "#141922", "BG_CARD": "#1F2733", "BG_SIDEBAR": "#1A2029",
        "GRID": "#2C3440", "TEXT_MAIN": "#EDF0F4", "TEXT_SUB": "#A6AEB9", "TEXT_MID": "#C9D0D9",
    },
    "light": {
        "BG_PAGE": "#FFFFFF", "BG_CARD": "#FFFFFF", "BG_SIDEBAR": "#F0F2F6",
        "GRID": "#E5E7EB", "TEXT_MAIN": "#1F2937", "TEXT_SUB": "#6B7280", "TEXT_MID": "#374151",
    },
}
_cur_theme = "light"


def set_theme(mode: str) -> None:
    """按 Streamlit 当前主题切换图表中性色。mode: 'dark' / 'light'"""
    global _cur_theme
    _cur_theme = mode if mode in THEME_COLORS else "light"


def _tc(name: str) -> str:
    return THEME_COLORS[_cur_theme][name]


def _base_layout(title: str, height: int = 420):
    return dict(
        title=dict(text=title, font=dict(size=15, color=_tc("TEXT_MAIN"))),
        template="plotly_dark" if _cur_theme == "dark" else "plotly",
        height=height,
        margin=dict(l=40, r=20, t=45, b=30),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.12, x=0, font=dict(size=11, color=_tc("TEXT_MID"))),
        paper_bgcolor=_tc("BG_PAGE"),
        plot_bgcolor=_tc("BG_CARD"),
        font=dict(color=_tc("TEXT_MID")),
    )


def _style_axes(fig) -> None:
    """统一网格线/轴线配色（兼容多子图，随主题）。"""
    for ax in fig.select_xaxes():
        ax.update(gridcolor=_tc("GRID"), zerolinecolor=_tc("GRID"), linecolor=_tc("GRID"))
    for ay in fig.select_yaxes():
        ay.update(gridcolor=_tc("GRID"), zerolinecolor=_tc("GRID"), linecolor=_tc("GRID"))


def equity_chart(result: BacktestResult, df: pd.DataFrame) -> go.Figure:
    """策略权益 vs 持股不动基准 vs 初始资金。"""
    fig = go.Figure()
    eq = result.equity
    fig.add_trace(go.Scatter(x=eq["time"], y=eq["equity"], name="策略权益",
                             line=dict(color=COLOR_ACCENT, width=2),
                             fill="tozeroy", fillcolor="rgba(0,180,216,0.10)"))
    bh = buy_and_hold_equity(result, df)
    if len(bh):
        fig.add_trace(go.Scatter(x=bh.index, y=bh.values, name="持股不动",
                                 line=dict(color="#9E9E9E", width=1.5, dash="dot")))
    fig.add_hline(y=result.init_cash, line_dash="dash", line_color="#5A6472",
                  annotation_text="初始资金", annotation_font_size=11)
    fig.update_layout(**_base_layout("账户权益曲线（策略 vs 持股不动）"))
    _style_axes(fig)
    fig.update_yaxes(tickformat=",.0f")
    return fig


def drawdown_chart(result: BacktestResult) -> go.Figure:
    eq = result.equity
    peak = eq["equity"].cummax()
    dd = (eq["equity"] - peak) / peak * 100
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=eq["time"], y=dd, fill="tozeroy", name="回撤(%)",
                             line=dict(color=COLOR_DOWN, width=1)))
    fig.update_layout(**_base_layout("回撤曲线", height=260))
    _style_axes(fig)
    return fig


def daily_t_chart(daily: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_bar(x=daily["日期"], y=daily["T净收益"], name="每日T净收益",
                marker_color=np.where(daily["T净收益"] >= 0, COLOR_UP, COLOR_DOWN))
    fig.add_trace(go.Scatter(x=daily["日期"], y=daily["累计T净收益"], name="累计T净收益",
                             line=dict(color=COLOR_ACCENT, width=2), yaxis="y2"))
    fig.update_layout(**_base_layout("每日 T 净收益与累计曲线", height=320))
    fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False))
    _style_axes(fig)
    return fig


def t_pnl_hist(series: pd.Series) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=series, nbinsx=30,
                               marker_color=COLOR_ACCENT, name="T单笔盈亏"))
    fig.add_vline(x=0, line_dash="dash", line_color="#FF5252")
    fig.update_layout(**_base_layout("单次 T 盈亏分布", height=320),
                      bargap=0.05)
    _style_axes(fig)
    return fig


def kline_chart(df_day: pd.DataFrame, trades_day: pd.DataFrame, code: str,
                adj_dates=None) -> go.Figure:
    """当日 1 分钟 K 线 + 盘中累计 VWAP + 买卖点 + 成交量。"""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.72, 0.28],
                        vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(
        x=df_day["time"], open=df_day["open"], high=df_day["high"],
        low=df_day["low"], close=df_day["close"], name="1分钟K线",
        increasing_line_color=COLOR_UP, increasing_fillcolor=COLOR_UP,
        decreasing_line_color=COLOR_DOWN, decreasing_fillcolor=COLOR_DOWN), row=1, col=1)

    # 盘中累计 VWAP
    cum_amt = df_day["amount"].cumsum()
    cum_vol = df_day["volume"].cumsum()
    vwap = cum_amt / cum_vol.replace(0, np.nan)
    fig.add_trace(go.Scatter(x=df_day["time"], y=vwap, name="盘中VWAP",
                             line=dict(color=COLOR_YELLOW, width=1.2)), row=1, col=1)

    # 买卖点
    if len(trades_day):
        buy = trades_day[trades_day["direction"] == "BUY"]
        sell = trades_day[trades_day["direction"] == "SELL"]
        tail = trades_day[(trades_day["direction"] == "SELL") &
                          trades_day["reason"].astype(str).str.contains("尾盘")]
        if len(buy):
            fig.add_trace(go.Scatter(x=buy["time"], y=buy["price"], mode="markers",
                                     name="买入B", marker=dict(symbol="triangle-up",
                                     size=12, color=COLOR_UP),
                                     text=["B"] * len(buy), textposition="top center",
                                     textfont=dict(color="#FFFFFF", size=10)),
                           row=1, col=1)
        if len(sell):
            fig.add_trace(go.Scatter(x=sell["time"], y=sell["price"], mode="markers",
                                     name="卖出S", marker=dict(symbol="triangle-down",
                                     size=12, color=COLOR_DOWN),
                                     text=["S"] * len(sell), textposition="bottom center",
                                     textfont=dict(color="#FFFFFF", size=10)),
                           row=1, col=1)
        if len(tail):
            fig.add_trace(go.Scatter(x=tail["time"], y=tail["price"], mode="markers",
                                     name="尾盘平仓T", marker=dict(symbol="diamond",
                                     size=11, color=COLOR_YELLOW),
                                     text=["T"] * len(tail), textposition="top center",
                                     textfont=dict(color="#FFFFFF", size=10)),
                           row=1, col=1)

    fig.add_trace(go.Bar(x=df_day["time"], y=df_day["volume"], name="成交量",
                         marker_color="#4A5568"), row=2, col=1)
    if adj_dates:
        day = pd.to_datetime(df_day["time"]).dt.date.iloc[0]
        if day in [pd.Timestamp(d).date() for d in adj_dates]:
            fig.add_vline(x=df_day["time"].iloc[0], line_dash="dot",
                          line_color="#FFC107", opacity=0.7,
                          annotation_text="疑似除权日", annotation_position="top right")
    fig.update_layout(**_base_layout(f"{code} 当日 1 分钟K线与买卖点", height=680))
    fig.update_xaxes(rangeslider_visible=False)
    _style_axes(fig)
    return fig


def macd_rsi_chart(df_day: pd.DataFrame, dif: np.ndarray, dea: np.ndarray,
                   bar: np.ndarray, rsi: np.ndarray) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.55, 0.45],
                        vertical_spacing=0.05)
    t = df_day["time"]
    colors = [COLOR_UP if b >= 0 else COLOR_DOWN for b in bar]
    fig.add_trace(go.Bar(x=t, y=bar, name="MACD柱", marker_color=colors), row=1, col=1)
    fig.add_trace(go.Scatter(x=t, y=dif, name="DIF", line=dict(color=COLOR_ACCENT, width=1.2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=t, y=dea, name="DEA", line=dict(color=COLOR_YELLOW, width=1.2)), row=1, col=1)
    fig.add_trace(go.Scatter(x=t, y=rsi, name="RSI(14)", line=dict(color="#B39DDB", width=1.2)), row=2, col=1)
    fig.add_hline(y=70, line_dash="dash", line_color="#FF5252", row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="#26A69A", row=2, col=1)
    fig.update_layout(**_base_layout("MACD / RSI", height=360))
    _style_axes(fig)
    return fig


def compare_equity_chart(eq_wide: pd.DataFrame) -> go.Figure:
    """参数组对比：多组权益曲线叠加。"""
    fig = go.Figure()
    colors = ["#00B4D8", "#FF5252", "#26A69A", "#FFC107", "#B39DDB",
              "#FF8A65", "#4FC3F7", "#AED581"]
    for idx, col in enumerate(eq_wide.columns):
        if col == "time":
            continue
        fig.add_trace(go.Scatter(x=eq_wide["time"], y=eq_wide[col], name=col,
                                 line=dict(width=2, color=colors[idx % len(colors)])))
    fig.update_layout(**_base_layout("参数组权益曲线对比", height=420))
    _style_axes(fig)
    fig.update_yaxes(tickformat=",.0f")
    return fig


def batch_compare_chart(result_df: pd.DataFrame) -> go.Figure:
    """批量回测：按股票分组的策略收益柱状图。"""
    fig = go.Figure()
    colors = ["#00B4D8", "#FF5252", "#26A69A", "#FFC107", "#B39DDB", "#FF8A65"]
    for i, (strat, g) in enumerate(result_df.groupby("策略")):
        fig.add_trace(go.Bar(name=strat, x=g["股票"], y=g["总收益%"],
                             marker_color=colors[i % len(colors)],
                             text=g["总收益%"].round(1), textposition="outside"))
    fig.update_layout(**_base_layout("批量回测：各策略收益对比（%）", height=440))
    fig.add_hline(y=0, line_color="#5A6472")
    _style_axes(fig)
    return fig


def sensitivity_heatmap(pivot: pd.DataFrame) -> go.Figure:
    """参数敏感性热力图：x=买入阈值，y=卖出阈值，z=总收益率%。"""
    fig = go.Figure(go.Heatmap(
        x=[str(c) for c in pivot.columns],
        y=[str(r) for r in pivot.index],
        z=pivot.values,
        colorscale="RdYlGn",
        zmid=0,
        colorbar=dict(title="总收益%"),
        hovertemplate="卖%s × 买%s<br>收益 %{z:.2f}%<extra></extra>",
    ))
    fig.update_layout(**_base_layout("参数敏感性：买卖阈值扫描（总收益率%）", height=360))
    _style_axes(fig)
    return fig


def monthly_heatmap(matrix: pd.DataFrame) -> go.Figure:
    """月度收益热力图：行=年，列=月。"""
    fig = go.Figure(go.Heatmap(
        x=[f"{m}月" for m in matrix.columns],
        y=[str(y) for y in matrix.index],
        z=matrix.values,
        colorscale="RdYlGn",
        zmid=0,
        colorbar=dict(title="月收益%"),
        hovertemplate="%{y}年%{x} %{z:.2f}%<extra></extra>",
    ))
    fig.update_layout(**_base_layout("月度收益热力图", height=320))
    _style_axes(fig)
    return fig
