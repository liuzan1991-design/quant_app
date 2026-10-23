# -*- coding: utf-8 -*-
"""H16 监测项：复权锚点 `latest = aligned[-1]` 的路径依赖。

背景（`量化研究规范_纠偏记录.md` H16）
------------------------------------
`prepare_signal_prices` 用「所传 df 最后一根 bar 的复权因子」作前复权基准
⇒ 把 df 截断到 T，共同历史区间的 `signal_*` 整体乘一个常数（full-sample normalization 泄漏）。
H16 定级 **latent**：偏移真实存在，但两条策略的决策尺度不变（已由
`scripts/tool_truncation_causal_audit.py` 的 Part2/Part3 实测）。

本文件是**监测**，不是"断言无泄漏"：
  1. `test_h16_anchor_bias_still_present` —— 记录式监测：跨大额除权切点的偏移
     **必须仍显著非零**（证明机制没被无意改掉）。等 C3 的 PIT 修法落地后，
     此用例会失败 —— 那是**预期信号**：届时改成"偏移恰为 0"的阻断断言并更新基线。
  2. `test_h16_bias_zero_after_last_event` —— 闭式不变式：切点晚于末次复权事件时，
     偏移必须**恰为 0**。这条与修法无关，恒应通过。
  3. `test_h16_past_decisions_invariant_to_future_events`（slow）——
     核心安全性质：把未来（含大额除权）从数据里切掉，**过去区间的成交必须逐笔不变**。
     这是"latent 而非已发生污染"的直接证明，也是任何复权口径改动的护栏。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from core.data import load_data
from strategies import get_strategy

APP = Path(__file__).resolve().parent
LIVE_CSV = APP / "data" / "live" / "stock_300308_1m.csv"
FACTOR_CSV = APP / "data" / "factor_300308.csv"

CUT_BIG = pd.Timestamp("2024-06-01")      # 跨 2024-06-06 的 ~40% 除权
CUT_AFTER_EVENT = pd.Timestamp("2026-10-15")   # 晚于 2026-10-12 派息（末次事件）


def _factor() -> pd.Series:
    if not FACTOR_CSV.exists():
        pytest.skip(f"缺复权因子缓存 {FACTOR_CSV}")
    f = pd.read_csv(FACTOR_CSV, encoding="utf-8-sig", parse_dates=["date"])
    return f.set_index("date")["factor"].astype(float).sort_index()


def _bias(cut: pd.Timestamp) -> float:
    """截断偏置 = factor(cut)/factor(数据末端) − 1；负值表示截断后历史信号价被整体抬高。"""
    f = _factor()
    return float(f.loc[:cut].iloc[-1]) / float(f.iloc[-1]) - 1.0


def test_h16_anchor_bias_still_present() -> None:
    """监测：跨 40% 除权切点的偏移仍显著非零（机制存在，未被静默改掉）。

    ⚠️ 等 C3 的 PIT 修法落地后，本用例**必然失败** —— 那是预期信号，
    届时改为「偏移恰为 0」的阻断断言。此处只钉"量级"，不钉具体数值：
    偏置值本身会随新发生的除权变化（2026-09 实测 +40.4%，2026-10 缓存 −28.9%）。
    """
    bias = _bias(CUT_BIG)
    assert abs(bias) >= 0.20, (
        f"H16 监测：2024-06-01 切点的锚点偏移仅 {bias:+.2%}，已不再显著。\n"
        f"要么复权基准已改为 PIT/后复权（请把本用例升级为「偏移=0」的阻断断言 + 更新基线），\n"
        f"要么因子缓存异常（核对 data/factor_300308.csv）。"
    )


def test_h16_bias_zero_after_last_event() -> None:
    """闭式不变式：切点晚于末次复权事件 ⇒ 偏移恰为 0（无未来事件可泄漏）。"""
    f = _factor()
    last_event = f[f.diff().abs() > 1e-9].index[-1]
    cut = max(CUT_AFTER_EVENT, last_event)
    if cut < f.index[-1]:
        assert _bias(cut) == pytest.approx(0.0, abs=1e-12), (
            f"切点 {cut.date()} 晚于末次事件 {last_event.date()}，偏移应为 0"
        )


@pytest.mark.slow
def test_h16_past_decisions_invariant_to_future_events() -> None:
    """核心安全性质：切掉未来（含 40% 除权）后，过去区间的成交逐笔不变。

    若哪天这条变红 ⇒ 复权基准已经影响到了决策 ⇒ H16 必须从 latent 升级为实际缺陷。
    """
    if not LIVE_CSV.exists():
        pytest.skip(f"缺本地行情 {LIVE_CSV}")
    start, end_full, end_trunc = (pd.Timestamp("2023-01-01"), pd.Timestamp("2025-12-31"),
                                  pd.Timestamp("2024-05-31"))
    df, _ = load_data("300308", str(start.date()), str(end_full.date()))
    df["time"] = pd.to_datetime(df["time"])
    full = df[(df["time"] >= start) & (df["time"] <= end_full)].reset_index(drop=True)
    trunc = df[(df["time"] >= start) & (df["time"] <= end_trunc)].reset_index(drop=True)
    assert len(full) > 50_000 and len(trunc) > 20_000, "数据不足，用例失去意义"

    strat = get_strategy("grid_trade")
    p = strat.default_params()
    r_full = strat.run(full.copy(), 1_000_000.0, p, {"code": "300308"})
    r_trunc = strat.run(trunc.copy(), 1_000_000.0, p, {"code": "300308"})

    cut = end_trunc + pd.Timedelta(days=1)
    cols = ["time", "direction", "shares", "price"]
    a = r_full.trades.copy()
    a["time"] = pd.to_datetime(a["time"])
    a = a[a["time"] < cut][cols].reset_index(drop=True)
    b = r_trunc.trades.copy()
    b["time"] = pd.to_datetime(b["time"])
    b = b[b["time"] < cut][cols].reset_index(drop=True)

    assert len(a) > 0 and len(b) > 0, "区间内应有成交"
    assert len(a) == len(b), f"成交笔数不同：含未来 {len(a)} vs 截断 {len(b)}"
    diff = (a[["direction", "shares"]] != b[["direction", "shares"]]).any(axis=1).sum()
    assert diff == 0, f"过去区间有 {diff} 笔成交方向/股数不一致 ⇒ H16 已升级为实际缺陷"
    assert (a["time"].values == b["time"].values).all(), "成交时间序列不一致"
    assert (a["price"].to_numpy() - b["price"].to_numpy()).__abs__().max() < 1e-9, "成交价不一致"
