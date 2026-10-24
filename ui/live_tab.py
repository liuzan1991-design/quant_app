# -*- coding: utf-8 -*-
"""「实盘控制」标签页（H57-S2）：**只读仪表盘**。

设计原则（2026-10-25 拍板后重写）
--------------------------------
1. **界面只读**：只回答"实盘现在在发生什么"，不提供改标的 / 改策略 / 改参数 /
   启停进程的入口。理由：这四类改动一旦发生，观察期样本立即失去可比性；
   任何改动必须走 **CLI + mutation-validation**（回测基线 → 改动 → guard-off
   复现 → 回归），这条链在界面上无法完成——放按钮等于诱导跳过纪律。
   旧版曾在此处放「配置编辑器」，已按此原则移除。
2. **口径权威**：E1 累计回撤直接读 `daily_history.jsonl` 的 `cumulative_drawdown`，
   **不读** `daily_summary.json`。后者曾给出 −25.16%，真值 −3.56%（差约 7 倍），
   界面不能成为错误口径的第二传播点。
3. **读状态全部带兜底**：文件缺失 / 损坏 ⇒ 返回 None 或空，绝不让界面崩
   （界面崩了，人连"哪里坏了"都看不到）。
4. **纯函数与渲染分离**：`collect_status()` / `snapshot_table()` /
   `history_frame()` 可单测，`render_live()` 只负责画。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from core import live_config as lc
from core.data import stock_name

# 只影响成本/执行、不影响策略语义的参数（改了不污染样本可比性）
# 界面虽不提供改参入口，仍用于**标注**配置里的覆盖项属于哪一类。
RUNTIME_KEYS = {
    "slippage_bps", "commission_rate", "min_commission",
    "stamp_tax_rate", "transfer_fee_rate",
}


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 缺失/损坏都不该让界面崩
        return None


def _read_jsonl(p: Path) -> list[dict]:
    """逐行读 jsonl；坏行跳过而不是整份报废（一条坏记录不该毁掉整段历史）。"""
    out: list[dict] = []
    try:
        text = Path(p).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def collect_status(root: Path) -> dict:
    """汇总实盘状态（纯函数，可单测）。

    返回：
        {"heartbeat": {...}|None, "health": {...}|None, "alerts": {...},
         "applied": {...}|None,
         "units": [{"symbol","strategy","summary","state","watermark","history"}]}
    """
    root = Path(root)
    out = {
        "heartbeat": _read_json(root / "heartbeat.json"),
        "health": _read_json(root / "health.json"),
        "alerts": _read_json(root / "ALERT_ACTIVE.json") or {},
        "applied": _read_json(root / "config_applied.json"),
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
                "history": _read_jsonl(st_dir / "daily_history.jsonl"),
            })
    return out


def history_frame(unit: dict) -> pd.DataFrame:
    """把一个单元的 daily_history.jsonl 摊成表（按日期排序，去重）。"""
    rows = []
    for r in unit.get("history") or []:
        rows.append({
            "日期": r.get("date"),
            "权益": r.get("equity"),
            "当日盈亏": r.get("day_pnl"),
            "累计盈亏": r.get("cum_pnl"),
            "当日回撤%": (round(r["day_drawdown"] * 100, 3)
                          if r.get("day_drawdown") is not None else None),
            "累计回撤%": (round(r["cumulative_drawdown"] * 100, 3)
                          if r.get("cumulative_drawdown") is not None else None),
            "委托": r.get("orders"),
            "成交": r.get("fills"),
            "拒单": r.get("rejected_orders"),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.dropna(subset=["日期"]).drop_duplicates(subset=["日期"], keep="last")
    return df.sort_values("日期").reset_index(drop=True)


def latest_history(unit: dict) -> dict:
    """最后一条历史记录（E1 累计回撤的权威来源）。"""
    hist = unit.get("history") or []
    return hist[-1] if hist else {}


def snapshot_table(status: dict) -> pd.DataFrame:
    """把 units 摊平成一张表（纯函数，便于测试与导出）。"""
    rows = []
    for u in status.get("units", []):
        s, st_, wm = u["summary"], u["state"], u["watermark"]
        hist = latest_history(u)
        pos = (st_.get("position") or {}) if isinstance(st_, dict) else {}
        dq = s.get("data_quality") or {}
        cap = s.get("position_cap") or {}
        upto = wm.get("processed_upto_by_day") or {}
        # E1 累计回撤：优先 history（权威），summary 只作兜底
        cdd = hist.get("cumulative_drawdown")
        if cdd is None:
            cdd = s.get("cumulative_drawdown")
        rows.append({
            "标的": u["symbol"],
            "策略": u["strategy"],
            "日期": s.get("date") or hist.get("date") or "—",
            "权益": s.get("equity"),
            "现金": s.get("cash"),
            "累计盈亏": s.get("cum_pnl"),
            "持仓股数": pos.get("shares"),
            "持仓成本": pos.get("cost"),
            "累计回撤%": (round(cdd * 100, 2) if cdd is not None else None),
            "数据质量": dq.get("status") or "—",
            "缺口%": (round(dq["gap_pct"] * 100, 2) if dq.get("gap_pct") is not None else None),
            "集中度%": (round(cap["ratio"] * 100, 2) if cap.get("ratio") is not None else None),
            "水位天数": len(upto) if upto else 0,
            "水位末日": (max(upto) if upto else "—"),
        })
    return pd.DataFrame(rows)


def semantic_override_keys(strategy_id: str, overrides: dict) -> list[str]:
    """覆盖项里哪些属于「语义参数」（改动会使观察期样本不可比）。"""
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
    for k in ("breaker_state", "breaker_opens", "breaker_fails",
              "rebuild_count", "rebuild_rejected"):
        if k in hb:
            cards.append((k, str(hb.get(k)), "flat"))
    _metric_cards(cards)

    if alerts:
        st.warning("当前未清除告警：")
        for kind, item in alerts.items():
            lvl = item.get("level", "alert")
            st.markdown(f"- **{kind}**（{lvl}）since `{item.get('since', '—')}`："
                        f"{item.get('message', '')}")
    else:
        st.success("无未清除告警")


def _render_units(status: dict) -> None:
    df = snapshot_table(status)
    st.markdown('<div class="sidebar-sec">当前观察单元</div>', unsafe_allow_html=True)
    if df.empty:
        st.info("live_outputs 下没有可读的观察单元（未运行或路径为空）")
        return
    st.dataframe(df, width="stretch", hide_index=True)


def _render_unit_detail(unit: dict) -> None:
    """单个单元的权益/回撤走势 + 最近 10 日明细（全部来自 daily_history.jsonl）。"""
    title = f"{unit['symbol']} × {unit['strategy']}"
    df = history_frame(unit)
    with st.expander(f"📈 {title}　走势与每日明细", expanded=False):
        if df.empty:
            st.info("尚无 daily_history.jsonl 记录")
            return
        plot = df.set_index("日期")
        st.markdown('<div class="sidebar-sec">权益曲线</div>', unsafe_allow_html=True)
        st.line_chart(plot[["权益"]])
        st.markdown('<div class="sidebar-sec">累计回撤 %（E1，读 daily_history）</div>',
                    unsafe_allow_html=True)
        st.line_chart(plot[["累计回撤%"]])
        st.markdown('<div class="sidebar-sec">最近 10 个交易日</div>', unsafe_allow_html=True)
        st.dataframe(df.tail(10), width="stretch", hide_index=True)


def _render_applied(status: dict, cfg_path: Path) -> None:
    """只读展示"live 实际按什么在跑"：启动留痕 + 配置文件。"""
    st.divider()
    st.markdown('<div class="sidebar-sec">运行配置（只读）</div>', unsafe_allow_html=True)

    applied = status.get("applied")
    if applied:
        st.caption(f"最近一次 live 启动留痕：source = `{applied.get('source')}`"
                   f"　时间 = {str(applied.get('recorded_at', '—'))[:19]}")
        combos = applied.get("combos") or []
        if combos:
            st.dataframe(pd.DataFrame(combos), width="stretch", hide_index=True)
    else:
        st.caption("尚无启动留痕（S3 接线后首次 live 启动才会写 "
                   "`live_outputs/config_applied.json`）")

    cfg = lc.load(cfg_path)
    if cfg is None:
        st.info(f"`{cfg_path.name}` 不存在 ⇒ live 按源码 `COMBOS` 运行"
                "（**当前无任何外部配置，行为与接线前逐字节一致**）")
        return

    rows = []
    for p in cfg.positions:
        sem = semantic_override_keys(p.strategy_id, p.param_overrides or {})
        rows.append({
            "标的": p.code,
            "名称": stock_name(p.code),
            "策略": p.strategy_id,
            "启用": "✅" if p.enabled else "⛔",
            "参数覆盖": json.dumps(p.param_overrides or {}, ensure_ascii=False),
            "语义参数⚠️": "、".join(sem) if sem else "—",
            "备注": p.note or "—",
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(f"配置最后更新：{cfg.updated_at or '—'}")


def _render_ops_guide() -> None:
    """操作指引：界面能做什么、不能做什么、改动该走哪条路。"""
    st.divider()
    st.markdown('<div class="sidebar-sec">操作指引</div>', unsafe_allow_html=True)
    st.markdown(
        "- **本页只能看**：所有数据来自 `live_outputs/`，界面不写任何文件。\n"
        "- **要改标的 / 策略 / 参数**：走命令行 + 回归，不要走界面。"
        "改动会让观察期样本失去可比性，必须先有回测基线，改完必须 "
        "guard-off 复现 + 全量回归。\n"
        "- **要启停实盘**：改 Windows 计划任务 `QuantLiveRun`（界面不提供按钮，"
        "避免手滑停掉正在跑的观察盘）。\n"
        "- **S5 护栏（二次确认 / 审计留痕 / 权限）尚未实现**，在此之前"
        "界面不会开放任何写操作入口。")
    st.code(
        "# 改动后的回归（日常档）\n"
        "python -m pytest -q -m \"not external\"\n\n"
        "# 含慢档（改动 UI / 引擎后跑）\n"
        "python -m pytest -q -m \"not external\"  # 慢档为 slow 标记，另跑\n"
        "python -m pytest -q -m slow",
        language="bash")


def render_live(output_root: Path, config_path: Path) -> None:
    st.caption("只读仪表盘：呈现实盘正在发生什么。"
               "**改标的 / 改参数 / 启停请走命令行 + 回归**，界面不开放写操作。")
    status = collect_status(output_root)
    _render_health(status)
    _render_units(status)
    for u in status.get("units", []):
        _render_unit_detail(u)
    _render_applied(status, config_path)
    _render_ops_guide()
