# -*- coding: utf-8 -*-
"""H10/H11 修复验证：日报口径与水印持久化。"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import live_match_loop


def _observer(tmp_path: Path, equity: float) -> live_match_loop.Observer:
    account = SimpleNamespace(equity=lambda: equity, cash=1_000_000)
    broker = SimpleNamespace(account=account, orders={}, fills=[])
    store = SimpleNamespace(root=tmp_path)
    obs = live_match_loop.Observer(
        code="300308",
        strategy_id="grid_trade",
        symbol="300308.SZ",
        params={},
        broker=broker,
        store=store,
    )
    return obs


def test_daily_metrics_reset_only_when_day_changes(tmp_path: Path) -> None:
    obs = _observer(tmp_path, 1_000_000)
    obs.equity_high = 1_100_000

    obs.reset_daily_metrics("2026-10-17")
    assert obs.day_start_equity == 1_000_000
    assert obs.day_high_equity == 1_000_000
    assert obs.day_anchor_date == "2026-10-17"

    # 同日重启不能重置日内基准。
    obs.broker.account.equity = lambda: 900_000
    obs.reset_daily_metrics("2026-10-17")
    assert obs.day_start_equity == 1_000_000
    assert obs.day_high_equity == 1_000_000

    # 跨日重置日内基准，但全局峰值必须保持不变。
    obs.reset_daily_metrics("2026-10-18")
    assert obs.day_start_equity == 900_000
    assert obs.day_high_equity == 900_000
    assert obs.equity_high == 1_100_000


def test_summary_reports_daily_and_cumulative_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})
    obs = _observer(tmp_path, 950_000)
    obs.equity_high = 1_000_000
    obs.day_start_equity = 900_000
    obs.day_high_equity = 980_000
    obs.day_anchor_date = "2026-10-18"
    obs.max_drawdown_cumulative = -0.10

    payload = live_match_loop._write_daily_summary(obs)

    assert payload["day_pnl"] == pytest.approx(50_000.0)
    assert payload["cum_pnl"] == pytest.approx(-50_000.0)
    assert payload["day_drawdown"] == pytest.approx(round((950_000 - 980_000) / 980_000, 6))
    assert payload["cumulative_drawdown"] == pytest.approx((950_000 - 1_000_000) / 1_000_000)

    # H11：摘要后水印必须同步落盘，且保留当日锚点。
    watermark = json.loads((tmp_path / "watermark.json").read_text(encoding="utf-8"))
    assert watermark["day_anchor_date"] == "2026-10-18"
    assert watermark["day_start_equity"] == 900_000
    assert watermark["day_high_equity"] == 980_000
    assert watermark["max_drawdown_cumulative"] == pytest.approx(-0.10)
