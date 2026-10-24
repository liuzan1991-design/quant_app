# -*- coding: utf-8 -*-
"""「实盘控制」标签页（H57-S2）：看实盘状态 + 编辑 live 配置。

设计原则
--------
1. **只读展示 + 写配置**，不启动/停止任何进程（启停要等 S3 接线后才有意义）。
2. **读状态全部带兜底**：文件缺失/损坏 ⇒ 返回 None 或空，绝不让界面崩。
3. **纯函数与渲染分离**：`collect_status()` / `snapshot_table()` 可单测，
   `render_live()` 只负责画，便于快速验证。
4. **语义参数显式标注**：改这类参数会让观察期样本失去可比性（见 H57 第五节），
   UI 必须先提示再允许保存。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from core import live_config as lc
from core.data import search_stocks, stock_name
from strategies import STRATEGIES, get_strategy

# 只影响成本/执行、不影响策略语义的参数（可随时改，不污染样本）
RUNTIME_KEYS = {
    "slippage_bps", "commission_rate", "min_commission",
    "stamp_tax_rate", "transfer_fee_rate",
}


def _read_json(p: Path) -> dict | None:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 缺失/损坏都不该让界面崩
        return None


def collect_status(root: Path) -> dict:
    """汇总实盘状态（纯函数，可单测）。

    返回：
        {"heartbeat": {...}|None, "health": {...}|None, "alerts": {...},
         "units": [{"symbol","strategy","summary","state","watermark"}]}
    """
    root = Path(root)
    out = {
        "heartbeat": _read_json(root / "heartbeat.json"),
        "health": _read_json(root / "health.json"),
        "alerts": _read_json(root / "ALERT_ACTIVE.json") or {},
        "units": [],
    }
    if not root.exists():
        return out
    for sym_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if sym_dir.name.startswith("_") or sym_dir.name == "logs":
            continue
        for st_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
            out["units"].append({
                "symbol": sym_dir.name,
                "strategy": st_dir.name,
                "summary": _read_json(st_dir / "daily_summary.json") or {},
                "state": _read_json(st_dir / "paper_state.json") or {},
                "watermark": _read_json(st_dir / "watermark.json") or {},
            })
    return out


def snapshot_table(status: dict) -> pd.DataFrame:
    """把 units 摊平成一张表（纯函数，便于测试与导出）。"""
    rows = []
    for u in status.get("units", []):
        s, st_, wm = u["summary"], u["state"], u["watermark"]
        pos = (st_.get("position") or {}) if isinstance(st_, dict) else {}
        dq = s.get("data_quality") or {}
        cap = s.get("position_cap") or {}
        upto = wm.get("processed_upto_by_day") or {}
        rows.append({
            "标的": u["symbol"],
            "策略": u["strategy"],
            "日期": s.get("date") or "—",
            "权益": s.get("equity"),
            "现金": s.get("cash"),
            "累计盈亏": s.get("cum_pnl"),
            "持仓股数": pos.get("shares"),
            "持仓成本": pos.get("cost"),
            "数据质量": dq.get("status") or "—",
            "缺口%": (round(dq["gap_pct"] * 100, 2) if dq.get("gap_pct") is not None else None),
            "集中度%": (round(cap["ratio"] * 100, 2) if cap.get("ratio") is not None else None),
            "水位天数": len(upto) if upto else 0,
            "水位末日": (max(upto) if upto else "—"),
        })
    return pd.DataFrame(rows)


def semantic_override_keys(strategy_id: str, overrides: dict) -> list[str]:
    """本次覆盖里哪些属于「语义参数」（改动会使观察期样本不可比）。"""
    return sorted(k for k in (overrides or {}) if k not in RUNTIME_KEYS)


# ── 渲染 ─────────────────────────────────────────────────────────────────────

def _metric_cards(cards: list[tuple[str, str, str]]) -> None:
    html = '<div class="metric-grid">' + "".join(
        f'<div class="metric-card"><div class="k">{k}</div>'
        f'<div class="v {c}">{v}</div></div>' for k, v, c in cards) + "</div>"
    st.markdown(html, unsafe_allow_html=True)


def _render_health(status: dict) -> None:
    hb = status.get("heartbeat") or {}
    alerts = status.get("alerts") or {}

    last_succ = hb.get("last_success_at") or "—"
    cards = [
        ("进程状态", str(hb.get("status") or "—"), "flat"),
        ("心跳日期", str(hb.get("date") or "—"), "flat"),
        ("最近采集成功", str(last_succ)[:19], "flat"),
        ("采集失败次数", str(hb.get("collect_errors", "—")), "flat"),
        ("活跃告警", str(len(alerts)), "down" if alerts else "up"),
    ]
    for k in ("breaker_state", "breaker_opens", "rebuild_count", "rebuild_rejected"):
        if k in hb:
            cards.append((k, str(hb.get(k)), "flat"))
    _metric_cards(cards)

    if alerts:
        st.warning("当前未清除告警：")
        for kind, item in alerts.items():
            lvl = item.get("level", "alert")
            st.markdown(f"- **{kind}**（{lvl}）since `{item.get('since', '—')}`：{item.get('message', '')}")
    else:
        st.success("无未清除告警")


def _render_units(status: dict) -> None:
    df = snapshot_table(status)
    st.markdown('<div class="sidebar-sec">当前观察单元</div>', unsafe_allow_html=True)
    if df.empty:
        st.info("live_outputs 下没有可读的观察单元（未运行或路径为空）")
        return
    st.dataframe(df, width="stretch", hide_index=True)


def _param_controls(strategy, current: dict, key_prefix: str) -> dict:
    """按 ParamMeta 生成控件（与 app.py 侧边栏同一套元数据）。"""
    out = {}
    meta = getattr(strategy, "param_meta", None) or []
    if not meta:
        st.caption("该策略未声明 ParamMeta，暂不支持图形化改参")
        return out
    cols = st.columns(2)
    for i, m in enumerate(meta):
        cur = current.get(m.key, m.default)
        tag = "" if m.key in RUNTIME_KEYS else " ⚠️"
        label = f"{m.label}{tag}"
        with cols[i % 2]:
            if m.kind == "checkbox":
                out[m.key] = st.checkbox(label, value=bool(cur), key=f"{key_prefix}_{m.key}",
                                         help=m.help)
            elif m.kind == "select" and m.options:
                opts = list(m.options)
                idx = opts.index(cur) if cur in opts else 0
                out[m.key] = st.selectbox(label, opts, index=idx,
                                          key=f"{key_prefix}_{m.key}", help=m.help)
            elif m.kind == "slice":      # 兼容未知 kind
                out[m.key] = st.text_input(label, value=str(cur),
                                           key=f"{key_prefix}_{m.key}", help=m.help)
            else:
                out[m.key] = st.number_input(
                    label, value=float(cur) if isinstance(cur, (int, float)) else 0.0,
                    min_value=float(m.min) if m.min is not None else None,
                    max_value=float(m.max) if m.max is not None else None,
                    step=float(m.step) if m.step else 1.0,
                    key=f"{key_prefix}_{m.key}", help=m.help)
    return out


def render_config_editor(cfg_path: Path) -> None:
    """编辑 data/live_config.json。保存时走 live_config.save() 的校验。"""
    st.markdown('<div class="sidebar-sec">配置（标的 / 策略 / 参数 / 启停）</div>',
                unsafe_allow_html=True)
    cfg = lc.load(cfg_path)
    if cfg is None:
        st.info("还没有 `data/live_config.json`。下面的编辑会在保存时创建。"
                "（不创建时 live 仍按源码里的 COMBOS 运行）")
        cfg = lc.LiveConfig()

    # ① 现有单元：启停 + 删除
    if cfg.positions:
        st.caption("勾选 = 启用（`enabled=false` 的组合不会被 live 执行）")
        for i, p in enumerate(list(cfg.positions)):
            c1, c2, c3 = st.columns([1.2, 4, 1])
            with c1:
                p.enabled = st.checkbox("实盘·启用", value=p.enabled, key=f"en_{i}")
            with c2:
                st.markdown(f"`{p.code}` {stock_name(p.code)} × `{p.strategy_id}`"
                            + (f"　*{p.note}*" if p.note else ""))
            with c3:
                if st.button("删除", key=f"del_{i}"):
                    cfg.positions.pop(i)
                    lc.save(cfg, cfg_path)
                    st.rerun()
    else:
        st.caption("暂无配置单元")

    # ② 新增单元
    with st.expander("＋ 新增一个「标的 × 策略」单元"):
        q = st.text_input("搜索全部 A 股（代码/名称）", key="live_q",
                          placeholder="如 300308、中际旭创")
        code = ""
        if q.strip():
            rows = search_stocks(q, 20)
            if rows:
                labels = {r["code"]: f"{r['code']} {r['name']}"
                          + ("（本地有数据）" if r.get("has_data") else "") for r in rows}
                code = st.selectbox("实盘·搜索结果", list(labels), format_func=lambda c: labels[c],
                                    key="live_code_sel")
            else:
                st.warning("没有匹配，试试 6 位代码")
        sid = st.selectbox("实盘·策略", list(STRATEGIES), key="live_sid",
                           format_func=lambda s: f"{get_strategy(s).name}（{s}）")
        strategy = get_strategy(sid)
        st.caption(strategy.description)
        base = strategy.default_params()
        edited = _param_controls(strategy, base, key_prefix=f"new_{sid}")
        overrides = {k: v for k, v in edited.items()
                     if k in base and v != base[k]}
        if overrides:
            sem = [k for k in overrides if k not in RUNTIME_KEYS]
            if sem:
                st.warning(f"⚠️ 这些是**语义参数**，改动会使观察期样本不可比：{sem}")
        note = st.text_input("实盘·备注（可选）", key="live_note")

        if st.button("加入配置", type="primary", key="live_add"):
            if not code:
                st.error("请先选择标的")
            else:
                cfg.positions.append(lc.LivePosition(code=code, strategy_id=sid,
                                                     enabled=True,
                                                     param_overrides=overrides,
                                                     note=note))
                errs = lc.validate(cfg)
                if errs:
                    cfg.positions.pop()
                    st.error("校验未通过：\n" + "\n".join(f"- {e}" for e in errs))
                else:
                    lc.save(cfg, cfg_path)
                    st.success(f"已加入 {code} × {sid}（{len(overrides)} 个参数覆盖）")
                    st.rerun()

    # ③ 保存启停变更
    if cfg.positions:
        if st.button("保存启停状态", key="live_save"):
            errs = lc.validate(cfg)
            if errs:
                st.error("校验未通过：\n" + "\n".join(f"- {e}" for e in errs))
            else:
                lc.save(cfg, cfg_path)
                st.success("已保存")

    if cfg_path.exists():
        st.caption(f"配置文件：`{cfg_path}`　最后更新：{cfg.updated_at or '—'}")


def render_live(output_root: Path, config_path: Path) -> None:
    st.caption("只读展示实盘状态；配置改动写入 `data/live_config.json`。"
               "**启停进程需 S3 接线后支持**，当前仅落配置。")
    status = collect_status(output_root)
    _render_health(status)
    _render_units(status)
    st.divider()
    render_config_editor(config_path)
