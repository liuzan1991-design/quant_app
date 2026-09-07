# -*- coding: utf-8 -*-
"""把批量回测 CSV 生成自包含 HTML 存档报告。"""
from datetime import datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio

APP_DIR = Path(__file__).resolve().parent
CSV = APP_DIR / "reports" / "batch_2023_5sectors.csv"

UP = "#FF5252"
DOWN = "#26A69A"
ACCENT = "#00B4D8"


def bar_compare(df: pd.DataFrame) -> go.Figure:
    """三策略收益分组柱状图（按股票）。"""
    fig = go.Figure()
    colors = {"日内做T（指标加权）": ACCENT, "网格交易": "#B39DDB",
              "大盘情绪做T": UP}
    for strat, g in df.groupby("策略"):
        fig.add_trace(go.Bar(name=strat, x=g["股票"], y=g["总收益%"],
                             marker_color=colors.get(strat, "#888"),
                             text=g["总收益%"].round(1), textposition="outside"))
    fig.update_layout(
        title="2023-01 ~ 2024-01 三策略收益对比（%）", template="plotly_dark",
        paper_bgcolor="#141922", plot_bgcolor="#1F2733", font=dict(color="#C9D0D9"),
        barmode="group", height=480, margin=dict(l=40, r=20, t=50, b=80),
        legend=dict(orientation="h", y=1.1))
    fig.add_hline(y=0, line_color="#5A6472")
    return fig


def bar_excess(df: pd.DataFrame) -> go.Figure:
    """策略超额收益柱状图（策略收益 - 股票涨跌）。"""
    fig = go.Figure()
    for strat, g in df.groupby("策略"):
        exc = g["总收益%"] - g["股票涨跌%"]
        fig.add_trace(go.Bar(name=strat, x=g["股票"], y=exc,
                             marker_color=ACCENT if strat == "大盘情绪做T" else "#4A5568",
                             text=exc.round(1), textposition="outside"))
    fig.update_layout(
        title="策略相对股票本身涨跌的超额收益（%）", template="plotly_dark",
        paper_bgcolor="#141922", plot_bgcolor="#1F2733", font=dict(color="#C9D0D9"),
        barmode="group", height=420, margin=dict(l=40, r=20, t=50, b=80),
        legend=dict(orientation="h", y=1.1))
    fig.add_hline(y=0, line_color="#5A6472")
    return fig


def main():
    df = pd.read_csv(CSV, encoding="utf-8-sig")
    figs = [bar_compare(df), bar_excess(df)]
    html_figs = "".join(pio.to_html(f, include_plotlyjs=(i == 0), full_html=False)
                        for i, f in enumerate(figs))

    def tbl(g):
        head = "<thead><tr>" + "".join(f"<th>{c}</th>" for c in g.columns) + "</tr></thead>"
        body = ""
        for _, r in g.iterrows():
            cls = ""
            if "涨跌" in "".join(g.columns) and "总收益" in g.columns:
                cls = ' class="up"' if r["总收益%"] > 0 else (' class="down"' if r["总收益%"] < 0 else "")
            body += "<tr>" + "".join(
                f"<td>{'' if pd.isna(v) else v}</td>" for v in r) + "</tr>"
        return f"<table>{head}<tbody>{body}</tbody></table>"

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>2023年五板块×三策略批量回测存档</title><style>
body {{ background:#141922; color:#EDF0F4; font-family:"Microsoft YaHei",sans-serif; padding:24px; }}
h1 {{ color:#00B4D8; font-size:22px; }} .meta {{ color:#A6AEB9; font-size:13px; margin-bottom:18px; }}
h2 {{ color:#00B4D8; font-size:16px; border-bottom:1px solid #2C3440; padding-bottom:6px; margin-top:28px; }}
.concl {{ background:#1F2733; border:1px solid #2C3440; border-radius:10px; padding:14px 18px; line-height:1.9; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; background:#1F2733; }}
th,td {{ border:1px solid #2C3440; padding:5px 8px; text-align:left; }}
th {{ background:#1A2029; color:#A6AEB9; }} .up {{ color:#FF5252; }} .down {{ color:#26A69A; }}
</style></head><body>
<h1>2023年五板块 × 三策略 批量回测存档</h1>
<div class="meta">生成时间：{now} ｜ 区间：2023-01-01 ~ 2024-01-01 ｜ 初始资金：100 万 ｜
数据源：星耀数智官方 1 分钟（不复权）｜ 费用：万一免五 + 印花税千1 + 过户费万0.2</div>

<h2>一、核心结论</h2>
<div class="concl">
1. <b>2023 年是成长股大牛市</b>：中际旭创 +318%、中文在线 +164%、昆仑万维 +161%、寒武纪 +147%，
做T 策略严重跑输持股不动（高抛低吸在单边上涨里=卖飞）。大牛市应持股，不宜做T。<br>
2. <b>做T 的价值在震荡/下跌市</b>：比亚迪 -22% 时做T 仅 -0.55%（超额 +21pct）；嘉泽新能 -6% 时做T 基本打平。<br>
3. <b>温和上涨股适合做T</b>：北方华创 +9.3%，做T +10.1%（超额 +0.7%）。<br>
4. <b>趋势自适应（情绪做T升级）</b>：大牛股自动切持股模式，寒武纪 +14.1%→+34.6%、中际旭创 +10.4%→+15.9%；
震荡股（北方华创/新易盛）略降，可关 trend_mode。<br>
5. <b>选股（板块）比选策略重要</b>：成长板块收益远高于公用事业/汽车低波动股。
</div>

<h2>二、收益对比</h2>
<div class="chart">{html_figs}</div>

<h2>三、明细表</h2>
{tbl(df)}

</body></html>"""

    out = APP_DIR / "reports" / "batch_2023_report.html"
    out.write_text(html, encoding="utf-8")
    print("存档报告:", out, f"（{out.stat().st_size/1024/1024:.1f} MB）")


if __name__ == "__main__":
    main()
