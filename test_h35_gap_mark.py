# -*- coding: utf-8 -*-
"""H35 另一半：**数据不完整日的摘要必须带缺口标记**（缺口 ≥5% ⇒ 作废）。

背景：D11（2026-09-23）只采到 161/240 根，摘要照样写出且无任何标记 ⇒
外部读成"有摘要 = 数据完整"，残缺日被计入样本。
判据来自 E3：一分钟线每日 240 根，缺口 ≥5% 该日作废。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import live_match_loop
from core.data_quality import EXPECTED_1M_BARS, day_bar_stats


# ── 判定边界（纯函数）──
@pytest.mark.parametrize("bars,status,usable", [
    (240, "complete", True),
    (239, "gap", True),        # 0.42% 缺口 —— 可用但标记
    (235, "gap", True),        # 2.08%
    (228, "void", False),      # 恰好 5.0% ⇒ 作废（阈值含等号）
    (227, "void", False),
    (161, "void", False),      # D11 实测
    (0, "void", False),
    (None, "unknown", False),  # 取不到 ≠ 完整
])
def test_h35_gap_classification(bars, status, usable) -> None:
    got = day_bar_stats(bars)
    assert got["status"] == status
    assert got["usable_for_sample"] is usable
    assert got["expected"] == EXPECTED_1M_BARS


def test_h35_gap_pct_math() -> None:
    assert day_bar_stats(120)["gap_pct"] == pytest.approx(0.5)
    assert day_bar_stats(240)["gap_pct"] == 0.0
    assert day_bar_stats(300)["gap_pct"] == 0.0, "超出也不应有负缺口"


# ── 集成：摘要里真的写进了标记（机制存在 ≠ 被调用）──
def _observer(tmp_path: Path) -> live_match_loop.Observer:
    account = SimpleNamespace(equity=lambda: 1_000_000, cash=1_000_000)
    return live_match_loop.Observer(
        code="300308", strategy_id="grid_trade", symbol="300308.SZ", params={},
        broker=SimpleNamespace(account=account, orders={}, fills=[]),
        store=SimpleNamespace(root=tmp_path),
    )


def _fake_live(n_bars: int) -> pd.DataFrame:
    today = pd.Timestamp(datetime.now().date())
    idx = pd.date_range(today + pd.Timedelta(hours=9, minutes=30),
                        periods=n_bars, freq="1min")
    return pd.DataFrame({"time": idx, "open": 1.0, "high": 1.0, "low": 1.0,
                         "close": 1.0, "volume": 100, "amount": 100.0})


@pytest.mark.parametrize("n_bars,status", [(240, "complete"), (161, "void"), (235, "gap")])
def test_h35_summary_contains_quality(tmp_path, monkeypatch, n_bars, status) -> None:
    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})
    monkeypatch.setattr(live_match_loop, "_load_live", lambda code: _fake_live(n_bars))
    payload = live_match_loop._write_daily_summary(_observer(tmp_path))
    assert "data_quality" in payload, "摘要必须带缺口标记"
    assert payload["data_quality"]["status"] == status
    assert payload["data_quality"]["bars"] == n_bars


def test_h35_count_failure_is_unknown_not_complete(tmp_path, monkeypatch) -> None:
    """计数异常 ⇒ unknown（usable=False），绝不默认为"完整"。"""
    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})

    def _boom(code):
        raise OSError("csv 读不了")

    monkeypatch.setattr(live_match_loop, "_load_live", _boom)
    payload = live_match_loop._write_daily_summary(_observer(tmp_path))
    assert payload["data_quality"]["status"] == "unknown"
    assert payload["data_quality"]["usable_for_sample"] is False
