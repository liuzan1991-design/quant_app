# -*- coding: utf-8 -*-
"""量化可视化回测平台 —— Streamlit 入口。运行：streamlit run app.py"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.data import (STOCK_POOL, load_data, stock_name,  # noqa: E402
                       search_stocks, local_data_stocks, load_preset_pool)
from core import batch, data_fetch, param_store, report, sensitivity  # noqa: E402
from strategies import STRATEGIES, get_strategy, list_strategies  # noqa: E402
from ui import charts, tabs  # noqa: E402

st.set_page_config(page_title="量化可视化回测平台", layout="wide", page_icon="📈")


def inject_css(is_light: bool = True) -> None:
    """注入自定义样式（线框图配色：青蓝主色 + 红涨绿跌；侧边栏随主题带浅灰/深色）。"""
    sidebar_bg = "#E9EDF3" if is_light else "var(--secondary-background-color)"
    css = """
    <style>
    html, body, .stApp, [data-testid="stAppViewContainer"],
    [data-testid="stMain"], [data-testid="stHeader"] {
        background-color:var(--background-color, #FFFFFF) !important;
        color:var(--text-color);
    }
    [data-testid="stMainBlockContainer"] { background:transparent; }
    .metric-grid { display:grid; grid-template-columns:repeat(8,1fr); gap:12px; margin:4px 0 18px; }
    .metric-card { background:var(--secondary-background-color);
                   border:1px solid rgba(160,170,185,0.35); border-radius:10px; padding:14px 16px; }
    .metric-card .k { font-size:12px; color:var(--text-color); opacity:0.78; margin-bottom:6px; }
    .metric-card .v { font-size:22px; font-weight:700; color:var(--text-color); }
    .metric-card .v.up { color:#FF5252; }
    .metric-card .v.down { color:#26A69A; }
    .topbar { display:flex; align-items:center; justify-content:space-between;
              height:50px; padding:0 20px; background:var(--secondary-background-color);
              border:1px solid rgba(160,170,185,0.25); border-radius:10px; margin-bottom:16px; }
    .topbar .logo { font-size:17px; font-weight:700; color:var(--primary-color); letter-spacing:1px; }
    .topbar .right { font-size:12px; color:var(--text-color); opacity:0.75; }
    .topbar .dot { color:#26A69A; }
    .sidebar-sec { font-size:12px; color:var(--text-color); opacity:0.75;
                   margin:16px 0 4px; }
    .section-title { color:var(--primary-color); font-size:15px; font-weight:600;
                      margin:18px 0 8px; border-bottom:1px solid rgba(160,170,185,0.28); padding-bottom:6px; }
    .sub-title { color:var(--primary-color); font-size:14px; font-weight:600; margin:14px 0 6px; }
    div[data-testid="stSidebar"] { background-color:__SIDEBAR_BG__;
                                   border-right:1px solid rgba(160,170,185,0.22); }
    div[data-testid="stSidebar"] .stButton button { width:100%; }
    div[data-testid="stSidebar"] button[kind="primary"],
    div[data-testid="stSidebar"] button[data-testid="stBaseButton-primary"] {
        background:#00B4D8; color:#08131A; font-weight:600; border:none; }
    div[data-testid="stSidebar"] .stAppDeployButton,
    div[data-testid="stSidebar"] footer { display:none; }
    .stTabs [data-baseweb="tab"] { font-size:14px; padding:8px 18px; }
    .stTabs [aria-selected="true"] { color:var(--primary-color) !important; }
    .block-container { padding-top:1.5rem; }
    </style>
    """
    st.markdown(css.replace("__SIDEBAR_BG__", sidebar_bg), unsafe_allow_html=True)


def toggle_theme() -> None:
    """侧边栏浅色/深色切换（写回 config.toml 后刷新；默认跟随当前配置，不强制改回）。"""
    cfg_path = APP_DIR / ".streamlit" / "config.toml"
    try:
        cfg_text = cfg_path.read_text(encoding="utf-8")
        is_light = 'base = "light"' in cfg_text
    except Exception:  # noqa: BLE001
        cfg_text, is_light = "", False
    light = st.toggle("浅色模式", value=is_light, key="theme_light")
    if cfg_text and light != is_light:
        if light:
            new_text = (cfg_text
                .replace('base = "dark"', 'base = "light"')
                .replace('backgroundColor = "#141922"', 'backgroundColor = "#FFFFFF"')
                .replace('secondaryBackgroundColor = "#1A2029"', 'secondaryBackgroundColor = "#F0F2F6"')
                .replace('textColor = "#EDF0F4"', 'textColor = "#31333F"'))
        else:
            new_text = (cfg_text
                .replace('base = "light"', 'base = "dark"')
                .replace('backgroundColor = "#FFFFFF"', 'backgroundColor = "#141922"')
                .replace('secondaryBackgroundColor = "#F0F2F6"', 'secondaryBackgroundColor = "#1A2029"')
                .replace('textColor = "#31333F"', 'textColor = "#EDF0F4"'))
        cfg_path.write_text(new_text, encoding="utf-8")
        st.rerun()


PARAM_GROUPS = [("core", "核心参数", True), ("risk", "风控参数", False),
                ("cost", "交易成本", False)]


def render_param_controls(strategy) -> dict:
    """根据策略参数元数据分组生成侧边栏控件。"""
    params = {}
    metas = {}
    for meta in strategy.param_meta:
        metas.setdefault(meta.group, []).append(meta)
    for gkey, glabel, expanded in PARAM_GROUPS:
        group_metas = metas.get(gkey, [])
        if not group_metas:
            continue
        with st.sidebar.expander(glabel, expanded=expanded):
            for meta in group_metas:
                key = f"param_{meta.key}"
                if meta.kind == "checkbox":
                    params[meta.key] = st.checkbox(meta.label, value=bool(meta.default),
                                                   key=key, help=meta.help)
                elif meta.kind == "select":
                    params[meta.key] = st.selectbox(meta.label, meta.options or [],
                                                    index=(meta.options or [meta.default]).index(meta.default),
                                                    key=key, help=meta.help)
                elif meta.kind == "slider":
                    params[meta.key] = st.slider(
                        meta.label, float(meta.min if meta.min is not None else 0),
                        float(meta.max if meta.max is not None else 20),
                        float(meta.default), float(meta.step if meta.step is not None else 1),
                        key=key, help=meta.help)
                else:  # number
                    fmt = "%.5f" if isinstance(meta.default, float) and meta.default < 1 else None
                    params[meta.key] = st.number_input(
                        meta.label, value=float(meta.default),
                        min_value=float(meta.min) if meta.min is not None else None,
                        max_value=float(meta.max) if meta.max is not None else None,
                        step=float(meta.step) if meta.step is not None else 1.0,
                        format=fmt, key=key, help=meta.help)
    return params


def sidebar() -> tuple:
    with st.sidebar:
        st.markdown('<div style="font-size:20px;font-weight:700;color:#00B4D8;'
                    'letter-spacing:1px;margin-bottom:2px;">量化可视化回测平台</div>'
                    '<div style="font-size:12px;color:#A6AEB9;margin-bottom:6px;">'
                    '策略可插拔 · 本地运行 · 数据源：星耀数智</div>',
                    unsafe_allow_html=True)

        # 策略选择（从注册表动态读取）
        st.markdown('<div class="sidebar-sec">策略</div>', unsafe_allow_html=True)
        strategy_list = list_strategies()
        sid_labels = {s[0]: f"{s[1]}（{s[0]}）" for s in strategy_list}
        sid = st.selectbox("策略", list(sid_labels), format_func=lambda s: sid_labels[s])
        strategy = get_strategy(sid)
        st.caption(strategy.description)

        # ① 股票选择：预置池（可编辑 data/preset_pool.txt）+ 全量搜索
        st.markdown('<div class="sidebar-sec">① 股票选择</div>', unsafe_allow_html=True)
        q = st.text_input("搜索全部 A 股（留空则用预置池）", key="stock_q",
                          placeholder="代码/拼音首字母/名称，如 300、hwj、寒武纪")
        if q.strip():
            cache = st.session_state.get("_pool_cache")
            if cache is None or cache.get("q") != q:
                cache = {"q": q, "rows": search_stocks(q, 30)}
                st.session_state["_pool_cache"] = cache
            rows = cache["rows"]
            codes = [r["code"] for r in rows]
            labels = {r["code"]: f"{r['code']} {r['name']}"
                                 + ("（本地有数据）" if r["has_data"] else "")
                      for r in rows}
            if not codes:
                st.warning("没有匹配的股票，试试输入 6 位代码")
                code = ""
            else:
                code = st.selectbox("搜索结果", codes, index=0,
                                    format_func=lambda c: labels[c], key="search_sel")
        else:
            preset_rows = load_preset_pool()
            preset_codes = [r["code"] for r in preset_rows]
            preset_labels = {r["code"]: f"{r['code']} {r['name']}"
                                       + ("（本地有数据）" if r["has_data"] else "")
                             for r in preset_rows}
            if not preset_codes:
                st.warning("预置池为空，请在 data/preset_pool.txt 中添加，或用下方搜索")
                code = ""
            else:
                code = st.selectbox("预置股票池", preset_codes, index=0,
                                    format_func=lambda c: preset_labels[c],
                                    key="preset_sel")
        st.caption(f"当前股票：{code} {stock_name(code) if code else '—'}"
                   f"（预置池可编辑 data/preset_pool.txt）")

        # 回测区间
        st.markdown('<div class="sidebar-sec">② 回测区间</div>', unsafe_allow_html=True)
        today = date.today()
        default_start = today - timedelta(days=365)
        d_range = st.date_input("回测区间", [default_start, today])
        start = d_range[0].isoformat() if isinstance(d_range, list) and d_range else default_start.isoformat()
        end = d_range[1].isoformat() if isinstance(d_range, list) and len(d_range) > 1 else today.isoformat()
        # 交易日数量（对齐线框图：区间下方直接显示）
        try:
            _df, _ = _load(code, start, end)
            if len(_df):
                st.caption(f"交易日数量：{pd.to_datetime(_df['time']).dt.date.nunique()} 天")
            else:
                st.caption("交易日数量：—（暂无数据）")
        except Exception:  # noqa: BLE001
            pass

        # 资金
        st.markdown('<div class="sidebar-sec">③ 资金设置</div>', unsafe_allow_html=True)
        init_cash = float(st.number_input("初始资金(元)", value=1_000_000.0,
                                          min_value=10_000.0, step=100_000.0))

        # 策略参数（动态渲染）
        st.markdown('<div class="sidebar-sec">④ 策略参数</div>', unsafe_allow_html=True)
        params = render_param_controls(strategy)
        if st.sidebar.button("↺ 恢复默认参数", use_container_width=True):
            for k in list(st.session_state.keys()):
                if k.startswith("param_"):
                    del st.session_state[k]
            st.rerun()
        st.sidebar.caption("费用默认按“万一免五”（佣金万一、最低佣金0）设置，按实际账户修改")

        # 操作按钮（对齐线框图：开始回测 / 拉取数据 / 导出报告）
        run_clicked = st.sidebar.button("开始回测", type="primary", use_container_width=True)
        fetch_clicked = st.sidebar.button("拉取最新数据", use_container_width=True)
        report_clicked = st.sidebar.button("导出 HTML 报告", use_container_width=True)

        # 多股票对比（A 方案：独立多选，不影响单股详情）
        with st.sidebar.expander("多股票对比（同一参数）"):
            local_codes = local_data_stocks()
            local_labels = [f"{c} {stock_name(c)}" for c in local_codes]
            multi_sel = st.multiselect("选择股票（本地有数据）", local_codes,
                                       format_func=lambda c: local_labels[local_codes.index(c)],
                                       key="multi_sel")
            st.caption("其他股票请先在主区【拉取最新数据】")
            if st.button("运行对比", use_container_width=True, key="multi_stock_run"):
                if len(multi_sel) < 2:
                    st.warning("至少选 2 只股票")
                else:
                    st.session_state["multi_stock_req"] = list(multi_sel)

        # ⚙ 更多工具（高级功能收进折叠区，侧边栏主体与线框图一致）
        with st.sidebar.expander("⚙ 更多工具"):
            toggle_theme()
            sens_clicked = st.button("参数敏感性分析", use_container_width=True)
            st.markdown('<div class="sidebar-sec" style="margin-top:10px;">参数组对比</div>',
                        unsafe_allow_html=True)
            snap_name = st.text_input("快照名称", key="snap_name_input")
            if st.button("保存当前参数", use_container_width=True):
                if param_store.save_snapshot(snap_name, sid, params, code, start, end, init_cash):
                    st.success(f"已保存「{snap_name}」")
                    st.rerun()
                else:
                    st.warning("请输入快照名称")
            snap_names = param_store.list_snapshot_names()
            selected = st.multiselect("选择对比组", snap_names, key="snap_compare_select")
            if st.button("运行对比", use_container_width=True):
                if not selected:
                    st.warning("请先选择对比组")
                else:
                    st.session_state["compare_req"] = selected
            if st.button("删除选中快照", use_container_width=True):
                for n in selected:
                    param_store.delete_snapshot(n)
                st.rerun()

        # ── 批量回测（多股票 × 多策略）──
        with st.sidebar.expander("批量回测（多股×多策略）"):
            preset = load_preset_pool()
            pool_labels = [f"{p['code']} {p['name']}" for p in preset]
            batch_pool = st.multiselect("选择股票（预置池）", pool_labels,
                                        key="batch_pool")
            batch_extra = st.text_input("追加代码（逗号分隔）", "",
                                        key="batch_extra",
                                        help="如 600519,000001，不在预置池也能跑（自动拉数据）")
            batch_sids = st.multiselect("选择策略", list(STRATEGIES),
                                        key="batch_sids",
                                        format_func=lambda s: STRATEGIES[s].name)
            batch_clicked = st.button("开始批量回测", use_container_width=True,
                                      key="batch_run")

    return (code, start, end, init_cash, sid, params, run_clicked,
            fetch_clicked, report_clicked, sens_clicked,
            batch_pool, batch_extra, batch_sids, batch_clicked)


@st.cache_data(show_spinner="加载数据…")
def _load(code: str, start: str, end: str):
    return load_data(code, start, end)


def main() -> None:
    is_light = False
    try:
        is_light = 'base = "light"' in (APP_DIR / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    charts.set_theme("light" if is_light else "dark")  # 图表中性色跟随主题
    theme_label = "浅色主题" if is_light else "深色主题"

    (code, start, end, init_cash, sid, params, run_clicked,
     fetch_clicked, report_clicked, sens_clicked,
     batch_pool, batch_extra, batch_sids, batch_clicked) = sidebar()
    inject_css(is_light)
    st.markdown(
        f'<div class="topbar"><div class="logo">量化可视化回测平台</div>'
        f'<div class="right"><span class="dot">●</span> 数据源：星耀数智 已连接 '
        f'&nbsp;|&nbsp; {theme_label}</div></div>',
        unsafe_allow_html=True)

    st.session_state["code"] = code

    df, data_file = _load(code, start, end)
    if not len(df):
        st.warning(f"本地没有 {code} 在 {start} ~ {end} 的 1 分钟数据。"
                   "点击侧边栏【拉取最新数据】即可从星耀数智拉取。")
    else:
        st.caption(f"数据：{code} {stock_name(code)} | {len(df):,} 根 | "
                   f"{pd.to_datetime(df['time']).iloc[0]} → {pd.to_datetime(df['time']).iloc[-1]} | "
                   f"{pd.to_datetime(df['time']).dt.date.nunique()} 个交易日")

    # 参数签名：参数变化后需重新点击"开始回测"
    sig = json.dumps({"code": code, "start": start, "end": end,
                      "cash": init_cash, "sid": sid, "params": params},
                     ensure_ascii=False, sort_keys=True)

    # ── 批量回测执行 ──
    if batch_clicked:
        stocks = [(lab.split(" ")[0], "") for lab in batch_pool]
        for c in [x.strip() for x in batch_extra.split(",") if x.strip()]:
            if c not in [s[0] for s in stocks]:
                stocks.append((c, ""))
        if not stocks:
            st.warning("请至少选择一只股票")
        elif not batch_sids:
            st.warning("请至少选择一个策略")
        else:
            pbar = st.progress(0.0, text="准备批量回测…")
            try:
                bdf = batch.run_batch(
                    stocks, batch_sids, init_cash, start, end,
                    progress=lambda f, msg: pbar.progress(f, text=msg))
            finally:
                pbar.empty()
            st.session_state["batch"] = {
                "sig": json.dumps({"start": start, "end": end, "cash": init_cash,
                                   "stocks": stocks, "sids": batch_sids},
                                  ensure_ascii=False, sort_keys=True),
                "df": bdf,
            }
            st.success(f"批量回测完成：{len(stocks)} 只 × {len(batch_sids)} 个策略")

    batch_res = st.session_state.get("batch")
    if batch_res is not None:
        cur_stocks = [(lab.split(" ")[0], "") for lab in batch_pool] + \
                     [(x.strip(), "") for x in batch_extra.split(",") if x.strip()]
        cur_bsig = json.dumps({"start": start, "end": end, "cash": init_cash,
                               "stocks": cur_stocks, "sids": batch_sids},
                              ensure_ascii=False, sort_keys=True)
        if batch_res.get("sig") != cur_bsig:
            batch_res = None
            st.session_state.pop("batch", None)

    # ── 数据增量更新 ──
    if fetch_clicked:
        try:
            with st.spinner("正在从星耀数智拉取数据…"):
                added, total, msg = data_fetch.incremental_update(code, start, end)
            st.success(f"{msg}")
            _load.clear()
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"数据拉取失败：{e}")

    # ── HTML 报告导出 ──
    if report_clicked:
        result_now = st.session_state.get("result")
        if result_now is None:
            st.warning("请先点击【开始回测】，再导出报告")
        else:
            try:
                out = report.build_html_report(result_now, st.session_state.get("df", df),
                                               code, stock_name(code), start, end)
                st.success(f"报告已生成：{out}")
                st.download_button("下载 HTML 报告", out.read_bytes(),
                                   file_name=out.name, mime="text/html")
            except Exception as e:  # noqa: BLE001
                st.error(f"报告生成失败：{e}")

    # ── 参数敏感性分析 ──
    if sens_clicked:
        if not len(df):
            st.error("没有数据，无法分析")
        else:
            with st.spinner("扫描买卖阈值 ±1 共 9 组参数…"):
                res = sensitivity.run_sensitivity(df, init_cash, sid, params)
                res["sig"] = json.dumps({"code": code, "start": start, "end": end,
                                         "cash": init_cash, "sid": sid},
                                        ensure_ascii=False, sort_keys=True)
                st.session_state["sensitivity"] = res

    sens = st.session_state.get("sensitivity")
    if sens is not None:
        cur_sig = json.dumps({"code": code, "start": start, "end": end,
                              "cash": init_cash, "sid": sid},
                             ensure_ascii=False, sort_keys=True)
        if sens.get("sig") == cur_sig:
            with st.container(border=True):
                st.markdown('<div class="section-title">🔍 参数敏感性分析（卖出 × 买入阈值）</div>',
                            unsafe_allow_html=True)
                st.plotly_chart(charts.sensitivity_heatmap(sens["pivot"]), width="stretch")
                st.dataframe(sens["table"], hide_index=True, width="stretch")

    # ── 多股票对比执行（A 方案：同一参数，逐只回测叠加）──
    multi_req = st.session_state.pop("multi_stock_req", None)
    if multi_req:
        msig = json.dumps({"start": start, "end": end, "cash": init_cash, "sid": sid,
                           "params": params, "stocks": multi_req},
                          ensure_ascii=False, sort_keys=True)
        eq_wide = None
        mrows = []
        strategy = get_strategy(sid)
        with st.spinner("多股票对比中…"):
            for c in multi_req:
                df_c, _ = _load(c, start, end)
                if not len(df_c):
                    mrows.append({"股票": f"{c} {stock_name(c)}（无数据）",
                                  "总收益率%": "n/a", "年化%": "n/a",
                                  "最大回撤%": "n/a", "卡玛": "n/a",
                                  "T胜率%": "n/a", "手续费": "n/a"})
                    continue
                r = strategy.run(df_c, init_cash, params)
                if eq_wide is None:
                    eq_wide = pd.DataFrame({"time": r.equity["time"].values})
                eq_wide[c] = r.equity.set_index("time")["equity"].reindex(eq_wide["time"]).values
                mrows.append({
                    "股票": f"{c} {stock_name(c)}",
                    "总收益率%": round(r.total_return * 100, 2),
                    "年化%": round(r.annual_return * 100, 2),
                    "最大回撤%": round(r.max_drawdown * 100, 2),
                    "卡玛": round(r.calmar, 2) if np.isfinite(r.calmar) else "n/a",
                    "T胜率%": round(r.t_win_rate, 1) if r.t_sell_count else "n/a",
                    "手续费": round(r.total_fees, 2),
                })
        if eq_wide is not None:
            st.session_state["multi_stock"] = {"sig": msig, "equity": eq_wide,
                                               "metrics": pd.DataFrame(mrows),
                                               "stocks": multi_req}
            st.success(f"对比完成：{'、'.join(f'{c} {stock_name(c)}' for c in multi_req)}")

    mcomp = st.session_state.get("multi_stock")
    if mcomp is not None:
        cur_msig = json.dumps({"start": start, "end": end, "cash": init_cash, "sid": sid,
                               "params": params, "stocks": mcomp.get("stocks", [])},
                              ensure_ascii=False, sort_keys=True)
        if mcomp.get("sig") != cur_msig:
            mcomp = None
            st.session_state.pop("multi_stock", None)

    if mcomp is not None:
        with st.container(border=True):
            st.markdown('<div class="section-title">🆚 多股票对比（同一参数）</div>',
                        unsafe_allow_html=True)
            st.plotly_chart(charts.compare_equity_chart(mcomp["equity"]), width="stretch")
            st.dataframe(mcomp["metrics"], hide_index=True, width="stretch")

    # ── 参数组对比执行 ──
    compare_req = st.session_state.pop("compare_req", None)
    if compare_req:
        comp_sig = json.dumps({"code": code, "start": start, "end": end,
                               "cash": init_cash, "sid": sid, "selected": compare_req},
                              ensure_ascii=False, sort_keys=True)
        if not len(df):
            st.error("没有数据，无法对比")
        else:
            snaps = param_store.load_snapshots()
            eq_wide = None
            rows = []
            strategy = get_strategy(sid)
            for n in compare_req:
                snap = snaps.get(n)
                if snap is None or snap.get("strategy_id") != sid:
                    continue
                r = strategy.run(df, init_cash, snap["params"])
                if eq_wide is None:
                    eq_wide = pd.DataFrame({"time": r.equity["time"].values})
                eq_wide[n] = r.equity.set_index("time")["equity"].reindex(eq_wide["time"]).values
                rows.append({
                    "参数组": n,
                    "总收益率%": round(r.total_return * 100, 2),
                    "年化%": round(r.annual_return * 100, 2),
                    "最大回撤%": round(r.max_drawdown * 100, 2),
                    "卡玛": round(r.calmar, 2) if np.isfinite(r.calmar) else "n/a",
                    "T胜率%": round(r.t_win_rate, 1) if r.t_sell_count else "n/a",
                    "手续费": round(r.total_fees, 2),
                })
            if rows:
                st.session_state["compare"] = {
                    "sig": comp_sig,
                    "selected": compare_req,
                    "equity": eq_wide,
                    "metrics": pd.DataFrame(rows),
                }
                st.success(f"对比完成：{'、'.join(compare_req)}")

    compare = st.session_state.get("compare")
    if compare is not None:
        cur_sig = json.dumps({"code": code, "start": start, "end": end,
                              "cash": init_cash, "sid": sid,
                              "selected": compare.get("selected", [])},
                             ensure_ascii=False, sort_keys=True)
        if compare.get("sig") != cur_sig:
            compare = None
            st.session_state.pop("compare", None)

    if compare is not None:
        with st.container(border=True):
            st.markdown('<div class="section-title">⚖️ 参数组对比</div>',
                        unsafe_allow_html=True)
            st.plotly_chart(charts.compare_equity_chart(compare["equity"]), width="stretch")
            st.dataframe(compare["metrics"], hide_index=True, width="stretch")

    if run_clicked:
        if not len(df):
            st.error("没有数据，无法回测")
        else:
            with st.spinner("回测中…"):
                strategy = get_strategy(sid)
                result = strategy.run(df, init_cash, params)
                st.session_state["result"] = result
                st.session_state["run_sig"] = sig
                st.session_state["df"] = df
                st.session_state["data_file"] = str(data_file) if data_file else None

    result = st.session_state.get("result")
    df_cur = st.session_state.get("df", df)

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["📊 回测总览", "📈 K线分析", "🧾 交易明细", "🗄 数据管理", "📋 批量回测"])
    has_result = result is not None and st.session_state.get("run_sig") == sig and len(df_cur)
    with tab1:
        if has_result:
            tabs.render_overview(result, df_cur, code, start, end)
        else:
            st.info("在左侧设置好参数后，点击【开始回测】查看结果")
            if result is not None:
                st.caption("（参数已修改，点击【开始回测】更新结果）")
    with tab2:
        if has_result:
            tabs.render_kline(df_cur, result)
        else:
            st.info("先完成一次回测，再看 K 线分析")
    with tab3:
        if has_result:
            tabs.render_trades(result)
        else:
            st.info("先完成一次回测，再看交易明细")
    with tab4:
        if has_result:
            tabs.render_data(code, df_cur, st.session_state.get("data_file"), start, end)
        else:
            st.info("先完成一次回测，再看数据管理")
    with tab5:
        tabs.render_batch(batch_res["df"] if batch_res is not None else None)


if __name__ == "__main__":
    main()
