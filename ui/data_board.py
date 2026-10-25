# -*- coding: utf-8 -*-
"""「数据看板」页 —— 视觉与交互参照 free-stockdb 的「多周期股票数据视图看板」。

参照来源（2026-10-25 实拉仓库，非猜测）
------------------------------------
`free-stockdb/数据网页版.html` + `网页版示范.png`：

  标题「多周期股票数据视图看板」，右上角标数据源地址；
  一条白色卡片式筛选行：代码 / 开始日期 / 结束日期(可选) / 周期频率 / 复权类型 /
  [读取数据] 深色主按钮 + [导出 Excel/CSV] 次按钮；
  一行状态提示（"数据载入并合并成功！"）；
  表头行左「代码 [周期] 起 至 止」、右「N 行」；
  主体是斑马纹紧凑表格，数字右对齐，涨幅红绿。

本页忠实照搬**结构与视觉语言**，但数据换成你自己的本地分钟线（`data/live/` 优先），
并且**不伪造没有的字段** —— 他的表里有换手率/总股本/流通市值/PE/PB，那些需要股本与财务数据，
本地没有，所以不列空列装样子。

纯函数（`resample_bars` / `daily_aggregate` / `board_frame`）与渲染分离，可单测。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from core.data import stock_name

APP = Path(__file__).resolve().parents[1]

FREQS = {
    "日K (1d)": "1d",
    "1 分钟": "1min",
    "5 分钟": "5min",
    "15 分钟": "15min",
}

CSS = """
<style>
.dbtitle{display:flex;align-items:baseline;justify-content:space-between;
  margin:2px 0 14px;}
.dbtitle h1{font-size:20px;font-weight:600;color:#16222f;margin:0;letter-spacing:.5px;}
.dbtitle .src{font-size:12px;color:#8896a8;font-family:ui-monospace,Consolas,monospace;}
.dbstat{font-size:12.5px;color:#0f6e56;margin:6px 0 10px;}
.dbstat.warn{color:#b42318;}
.dbhead{display:flex;align-items:baseline;justify-content:space-between;
  font-size:12.5px;color:#5f6b7a;margin:2px 0 6px;}
.dbhead b{color:#16222f;font-weight:600;}
[data-testid="stVerticalBlockBorderWrapper"]{
  background:#ffffff;border:1px solid #e3ebf5 !important;border-radius:10px;
  box-shadow:0 1px 2px rgba(22,34,47,.04);
}
div[data-testid="stDataFrame"] div[role="columnheader"]{
  background:#eef4fb;color:#16222f;font-weight:600;
}
</style>
"""


def local_csv(code: str) -> Path | None:
    """本地分钟线：live 池优先（更新到当日），其次静态池。"""
    for p in (APP / "data" / "live" / f"stock_{code}_1m.csv",
              APP / "data" / f"stock_{code}_1m.csv"):
        if p.exists():
            return p
    return None


def load_local(code: str) -> pd.DataFrame:
    p = local_csv(code)
    if p is None:
        raise FileNotFoundError(f"本地没有 {code} 的分钟线")
    df = pd.read_csv(p, parse_dates=["time"])
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


def resample_bars(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """1m → 5m/15m/日K。**按自然日分组再重采样**，不跨隔夜（A 股不能跨日聚合）。"""
    if df.empty or freq == "1min":
        return df.copy()
    rule = "1D" if freq == "1d" else freq
    out = []
    for _, g in df.groupby(df["time"].dt.normalize(), sort=True):
        g = g.set_index("time")
        agg = g.resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                    "close": "last", "volume": "sum", "amount": "sum"})
        agg = agg.dropna(subset=["open"]).reset_index()
        out.append(agg)
    return (pd.concat(out, ignore_index=True).sort_values("time").reset_index(drop=True)
            if out else df.iloc[0:0].copy())


def board_frame(df: pd.DataFrame, code: str, freq: str) -> pd.DataFrame:
    """按参考页的列序排表：时间/代码/名称/开/高/低/收/前收/量/额/涨幅/振幅/量比。"""
    if df.empty:
        return pd.DataFrame()
    d = df.copy().sort_values("time").reset_index(drop=True)
    d["前收盘价"] = d["close"].shift(1)
    d.loc[d.index[0], "前收盘价"] = d.loc[d.index[0], "open"]
    d["涨幅%"] = (d["close"] / d["前收盘价"] - 1) * 100
    d["振幅%"] = (d["high"] - d["low"]) / d["前收盘价"] * 100

    if freq == "1d":
        # 首根没有"前 5 日"，分母退回自身 ⇒ 量比 1.0（不显示 None）
        base = d["volume"].rolling(5, min_periods=1).mean().shift(1)
        d["量比"] = (d["volume"] / base).fillna(1.0)
    else:
        # 分时口径下量比要按"每分钟均量 vs 前5日同时段"算，本地只到日粒度 ⇒ 不装样子
        d["量比"] = float("nan")

    tcol = "日期/时间"
    d[tcol] = (d["time"].dt.strftime("%Y%m%d") if freq == "1d"
               else d["time"].dt.strftime("%Y%m%d %H:%M"))
    out = pd.DataFrame({
        tcol: d[tcol],
        "代码": code,
        "名称": stock_name(code),
        "开盘价": d["open"].round(2),
        "最高价": d["high"].round(2),
        "最低价": d["low"].round(2),
        "收盘价": d["close"].round(2),
        "前收盘价": d["前收盘价"].round(2),
        "成交量": d["volume"].round(0),
        "成交额": d["amount"].round(0),
        "涨幅%": d["涨幅%"].round(2),
        "振幅%": d["振幅%"].round(2),
        "量比": d["量比"].round(2),
    })
    return out


def _render_board(code: str, freq_label: str, start, end) -> None:
    freq = FREQS[freq_label]
    try:
        raw = load_local(code)
    except FileNotFoundError:
        st.markdown(f'<div class="dbstat warn">本地没有 {code} 的分钟线，'
                    f'请先在「数据管理」补齐后重试。</div>', unsafe_allow_html=True)
        return

    sel = raw[(raw["time"].dt.date >= start) & (raw["time"].dt.date <= end)]
    if sel.empty:
        st.markdown('<div class="dbstat warn">该区间内没有数据，请调整日期。</div>',
                    unsafe_allow_html=True)
        return

    bars = resample_bars(sel, freq)
    table = board_frame(bars, code, freq)

    st.markdown(f'<div class="dbstat">数据载入并合并成功！'
                f'<span style="color:#8896a8;margin-left:10px;">本地实际覆盖至 '
                f'{sel["time"].max():%Y-%m-%d %H:%M}</span></div>', unsafe_allow_html=True)
    st.markdown(
        f'<div class="dbhead"><span><b>{code}</b> [{freq}] '
        f'{start:%Y-%m-%d} 至 {end:%Y-%m-%d}</span>'
        f'<span>{len(table)} 行</span></div>', unsafe_allow_html=True)

    st.dataframe(
        table, width="stretch", hide_index=True, height=520,
        column_config={
            "成交量": st.column_config.NumberColumn(format="%d"),
            "成交额": st.column_config.NumberColumn(format="%d"),
            "涨幅%": st.column_config.NumberColumn(format="%.2f"),
            "振幅%": st.column_config.NumberColumn(format="%.2f"),
            "量比": st.column_config.NumberColumn(format="%.2f"),
        })
    # 导出按钮在筛选卡片里（与参考页一致：按钮成对出现），这里只备好内容
    st.session_state["db_csv"] = table.to_csv(index=False)
    st.session_state["db_fname"] = f"{code}_{freq}_{start:%Y%m%d}_{end:%Y%m%d}.csv"


def render_board(default_code: str = "300308") -> None:
    """整页渲染：标题行 → 筛选卡片 → 状态 → 表。

    交互照搬参考页：**先填条件，点「读取数据」才出表**（不是每改一个控件就重读一遍）。
    """
    st.markdown(CSS, unsafe_allow_html=True)
    st.markdown(
        '<div class="dbtitle"><h1>多周期股票数据视图看板</h1>'
        '<span class="src">数据源：本地分钟线　AD/QMT</span></div>',
        unsafe_allow_html=True)

    with st.container(border=True):
        c1, c2, c3, c4, c5, c6, c7 = st.columns([1.3, 1.25, 1.35, 1.15, 1.05, 1.0, 1.05])
        with c1:
            code = st.text_input("代码", value=default_code, key="db_code",
                                 placeholder="如 300308")
        with c2:
            start = st.date_input("开始日期", value=pd.Timestamp("2026-09-01").date(),
                                  key="db_start")
        with c3:
            end = st.date_input("结束日期（可选）", value=pd.Timestamp("2026-10-23").date(),
                                key="db_end")
        with c4:
            freq_label = st.selectbox("周期频率", list(FREQS), key="db_freq")
        with c5:
            st.selectbox("复权类型", ["不复权"], key="db_fq",
                         help="本地存的是不复权实价；前/后复权需 AD 因子，故此处只给不复权。")
        with c6:
            st.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
            go = st.button("读取数据", type="primary", key="db_go", width="stretch")
        with c7:
            st.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
            if st.session_state.get("db_loaded"):
                st.download_button(
                    "导出 CSV", st.session_state["db_csv"].encode("utf-8-sig"),
                    file_name=st.session_state["db_fname"], mime="text/csv",
                    key="db_export", width="stretch")
            else:
                st.button("导出 CSV", key="db_export_off", disabled=True, width="stretch")

    if not (code or "").strip():
        st.info("请输入代码")
        return

    if go:
        st.session_state["db_loaded"] = {"code": code.strip(), "freq": freq_label,
                                         "start": start, "end": end}
    state = st.session_state.get("db_loaded")
    if not state:
        st.markdown('<div class="dbstat">填好条件后点「读取数据」。</div>',
                    unsafe_allow_html=True)
        return
    _render_board(state["code"], state["freq"], state["start"], state["end"])
    st.caption("注：换手率 / 股本 / 市值 / 市盈率 / 市净率 需股本与财务数据，本地无此来源，"
               "故不设空列。量比在日K口径下为「当日量 ÷ 前5日均量」。")
