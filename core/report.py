# -*- coding: utf-8 -*-
"""HTML 报告导出：自包含、可离线打开。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.io as pio

from core import metrics
from strategies.base import BacktestResult
from ui import charts

APP_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = APP_DIR / "reports"


def _fmt_params(params: dict) -> pd.DataFrame:
    rows = []
    for k, v in params.items():
        if isinstance(v, dict):
            v = json.dumps(v, ensure_ascii=False)
        rows.append({"参数": k, "值": v})
    return pd.DataFrame(rows)


def _html_table(df: pd.DataFrame, max_rows: Optional[int] = None) -> str:
    if not len(df):
        return "<p>无数据</p>"
    if max_rows and len(df) > max_rows:
        shown = df.head(max_rows)
        extra = len(df) - max_rows
    else:
        shown = df
        extra = 0
    head = "<thead><tr>" + "".join(f"<th>{c}</th>" for c in df.columns) + "</tr></thead>"
    body = ""
    for _, r in shown.iterrows():
        body += "<tr>" + "".join(
            f"<td>{'' if pd.isna(v) else v}</td>" for v in r) + "</tr>"
    tail = f'<tr><td colspan="{len(df.columns)}">… 共 {len(df)} 行，仅显示前 {max_rows} 行</td></tr>' if extra else ""
    return f"<table>{head}<tbody>{body}{tail}</tbody></table>"


def build_html_report(result: BacktestResult, df: pd.DataFrame, code: str,
                      stock: str, start: str, end: str) -> Path:
    """生成自包含 HTML 报告，返回保存路径。"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    daily = metrics.daily_t_summary(result)
    attr = metrics.attribution(result, df, daily)
    pf = metrics.profit_factor(result)

    figs = [
        charts.equity_chart(result, df),
        charts.drawdown_chart(result),
        charts.daily_t_chart(daily) if len(daily) else None,
        charts.t_pnl_hist(metrics.t_pnl_series(result)) if len(metrics.t_pnl_series(result)) else None,
    ]
    html_figs = ""
    for i, fig in enumerate(figs):
        if fig is None:
            continue
        html_figs += pio.to_html(fig, include_plotlyjs=(i == 0), full_html=False)

    metric_cards = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div></div>'
        for k, v in [
            ("总收益率", f"{result.total_return*100:+.2f}%"),
            ("年化收益", f"{result.annual_return*100:+.2f}%"),
            ("最大回撤", f"{result.max_drawdown*100:.2f}%"),
            ("卡玛比率", f"{result.calmar:.2f}" if result.calmar == result.calmar else "n/a"),
            ("交易次数", f"{result.buy_count+result.sell_count}"),
            ("T胜率", f"{result.t_win_rate:.1f}%" if result.t_sell_count else "n/a"),
            ("累计手续费", f"{result.total_fees:,.2f} 元"),
            ("盈亏比", f"{pf:.2f}" if pf != float("inf") else "∞"),
        ])

    attr_rows = "".join(
        f"<tr><td>{k}</td><td style='text-align:right'>{v:+,.2f} 元</td></tr>"
        for k, v in attr.items())

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    title = f"{stock}（{code}）日内做T回测报告"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<title>{title}</title>
<style>
  body {{ background:#0E1117; color:#E8EAED; font-family:"Microsoft YaHei",sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:22px; color:#00B4D8; margin-bottom:4px; }}
  .meta {{ color:#8B93A1; font-size:13px; margin-bottom:20px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }}
  .card {{ background:#1B1F27; border:1px solid #2A3040; border-radius:8px; padding:12px 14px; }}
  .k {{ font-size:12px; color:#8B93A1; }}
  .v {{ font-size:20px; font-weight:700; margin-top:4px; }}
  h2 {{ font-size:16px; color:#00B4D8; margin:24px 0 8px; border-bottom:1px solid #262B36; padding-bottom:6px; }}
  table {{ border-collapse:collapse; width:100%; font-size:12px; background:#1B1F27; }}
  th,td {{ border:1px solid #2A3040; padding:5px 8px; text-align:left; }}
  th {{ background:#11151C; color:#8B93A1; }}
  .chart {{ background:#0E1117; margin:12px 0; }}
  .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
</style></head><body>
<h1>{title}</h1>
<div class="meta">生成时间：{now} &nbsp;|&nbsp; 区间：{start} ~ {end} &nbsp;|&nbsp;
策略：{result.strategy_name} &nbsp;|&nbsp; 初始资金：{result.init_cash:,.0f} 元 &nbsp;|&nbsp; 数据：不复权</div>
<div class="cards">{metric_cards}</div>
<h2>收益归因</h2>
<table><tbody>{attr_rows}</tbody></table>
<h2>策略参数</h2>
{_html_table(_fmt_params(result.params))}
<div class="chart">{html_figs}</div>
<h2>交易明细（共 {len(result.trades)} 笔）</h2>
{_html_table(result.trades, max_rows=200)}
<h2>被拒委托（{len(result.rejected)} 条）</h2>
{_html_table(result.rejected, max_rows=100)}
</body></html>"""

    fname = f"report_{code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    out = REPORTS_DIR / fname
    out.write_text(html, encoding="utf-8")
    return out
