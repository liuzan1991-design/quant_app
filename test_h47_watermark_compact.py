# -*- coding: utf-8 -*-
"""H47：水印按「每日最高已处理时刻」压缩 —— 语义等价、体积从 MB 降到 KB。

红线：`processed_times` 是**去重的唯一依据**，任何压缩都不得改变"哪些 bar 已处理"。
本文件的等价性是靠构造边界 case 逐条比出来的，不是靠读代码推断。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import live_match_loop


def _session_times(day: str) -> list[pd.Timestamp]:
    """一个真实交易日的 240 根：09:30–11:29 + 13:00–14:59（无 11:30/15:00 标签）。"""
    am = pd.date_range(f"{day} 09:30", periods=120, freq="1min")
    pm = pd.date_range(f"{day} 13:00", periods=120, freq="1min")
    return list(am) + list(pm)


def _obs(tmp_path: Path) -> live_match_loop.Observer:
    account = SimpleNamespace(equity=lambda: 1_000_000, cash=1_000_000)
    return live_match_loop.Observer(
        code="300308", strategy_id="grid_trade", symbol="300308.SZ", params={},
        broker=SimpleNamespace(account=account, orders={}, fills=[]),
        store=SimpleNamespace(root=tmp_path),
    )


def _legacy_watermark(tmp_path: Path, times) -> Path:
    p = tmp_path / "watermark.json"
    p.write_text(json.dumps({
        "code": "300308", "strategy_id": "grid_trade",
        "processed_times": [pd.Timestamp(t).isoformat() for t in times],
        "equity_high": 1_234_567.0, "day_start_equity": 1_000_000.0,
        "day_anchor_date": "2026-10-23", "day_high_equity": 1_100_000.0,
        "max_drawdown_cumulative": -0.02,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def test_h47_semantics_equivalent_to_full_set() -> None:
    """按日水位判定 vs 全量集合判定 —— 对同一批 bar 结论必须逐条相同。"""
    days = ["2026-10-21", "2026-10-22", "2026-10-23"]
    processed = []
    for i, d in enumerate(days):     # 前两天整日、第三天半天
        ts = _session_times(d)
        processed += ts if i < 2 else ts[:120]
    obs = live_match_loop.Observer(code="300308", strategy_id="grid_trade",
                                   symbol="300308.SZ", params={})
    legacy_set = set(pd.to_datetime(processed))
    for t in processed:
        obs._mark_processed(pd.Timestamp(t))

    probe = _session_times("2026-10-21") + _session_times("2026-10-22") + _session_times("2026-10-23")
    got = [obs._is_processed(t) for t in probe]
    want = [t in legacy_set for t in probe]
    assert got == want, "按日水位与全量集合判定不一致"


def test_h47_boundary_exactly_upto_is_processed() -> None:
    obs = live_match_loop.Observer(code="c", strategy_id="grid_trade", symbol="c.SZ", params={})
    obs._mark_processed(pd.Timestamp("2026-10-23 10:00"))
    assert obs._is_processed(pd.Timestamp("2026-10-23 10:00")) is True, "边界必须算已处理"
    assert obs._is_processed(pd.Timestamp("2026-10-23 10:01")) is False
    assert obs._is_processed(pd.Timestamp("2026-10-22 14:59")) is False, "更早的日期不得误判"


def test_h47_legacy_file_migrates_and_shrinks(tmp_path: Path) -> None:
    """旧格式（21.9 万条列表）加载后应自动迁移成 KB 级，且标量字段一字不丢。"""
    days = pd.bdate_range("2025-01-01", periods=300).strftime("%Y-%m-%d")
    times = [t for d in days for t in _session_times(d)]
    p = _legacy_watermark(tmp_path, times)
    before = p.stat().st_size
    assert before > 1_000_000, "回归前提：旧文件确实很大"

    obs = _obs(tmp_path)
    obs.load_watermark()
    assert len(obs.day_upto) == 300, "应按日聚合为 300 个水位"
    assert obs.equity_high == pytest.approx(1_234_567.0)
    obs.save_watermark()
    after = p.stat().st_size
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "processed_upto_by_day" in data
    assert len(data["processed_upto_by_day"]) == 300
    assert len(data["processed_times"]) == 300, "落盘只保留每日最高值"
    assert after < 50_000, f"压缩后应到 KB 级：{after} 字节"
    assert after < before / 40, f"体积应降一个数量级以上：{before} → {after}"
    assert data["equity_high"] == pytest.approx(1_234_567.0), "标量字段不得丢失"


def test_h47_new_bars_only_increment(tmp_path: Path) -> None:
    """同一批 bar 处理两次，第二次必须 0 条（不能因为压缩而重放）。"""
    obs = _obs(tmp_path)
    df = pd.DataFrame({"time": pd.date_range("2026-10-23 09:30", periods=240, freq="1min")})
    for t in df["time"]:
        obs._mark_processed(t)
    obs.save_watermark()

    obs2 = _obs(tmp_path)
    obs2.load_watermark()
    times = pd.to_datetime(df["time"])
    upto = times.dt.normalize().dt.date.map(lambda d: obs2.day_upto.get(d))
    assert int((upto.notna() & (times <= upto)).sum()) == 240, "重启后 240 根全部视为已处理"


def test_h47_save_is_idempotent(tmp_path: Path) -> None:
    obs = _obs(tmp_path)
    for t in pd.date_range("2026-10-23 09:30", periods=10, freq="1min"):
        obs._mark_processed(t)
    obs.save_watermark()
    first = (tmp_path / "watermark.json").read_text(encoding="utf-8")
    obs.save_watermark()
    assert (tmp_path / "watermark.json").read_text(encoding="utf-8") == first
