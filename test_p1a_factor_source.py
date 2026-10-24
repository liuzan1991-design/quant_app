# -*- coding: utf-8 -*-
"""P1-a：复权因子可切换第二数据源（QMT）—— 方向/开关/回退/缓存隔离。

对应方案 `D:\\Codex输出\\P1-a接线方案_20261024.md` 的验证计划 #3-#6。
"""
from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from core import adjustment as adj

APP = Path(__file__).resolve().parent
AD_CSV = APP / "data" / "factor_300308.csv"


# ── #3 方向：QMT 事件 → 与 AD 同向的累计因子 ──
def test_p1a_direction_matches_ad_convention() -> None:
    dates = pd.DatetimeIndex(["2024-06-05", "2024-06-06", "2024-06-07"])
    f = adj.rebuild_factor_from_events([pd.Timestamp("2024-06-06")], [1.4], dates)
    assert f.loc["2024-06-05"] == pytest.approx(1 / 1.4), "事件前必须为 1/dr（与 AD 同向）"
    assert f.loc["2024-06-06"] == pytest.approx(1.0), "事件当日起不再乘"
    assert f.iloc[-1] == pytest.approx(1.0), "末值必须为 1.0"
    assert f.is_monotonic_increasing, "必须随 t 递增（否则 corporate_actions 的 pct_change 会反号）"

    f2 = adj.rebuild_factor_from_events(
        [pd.Timestamp("2024-06-06"), pd.Timestamp("2024-06-10")], [1.4, 1.0015],
        pd.DatetimeIndex(["2024-06-01", "2024-06-11"]))
    assert f2.iloc[0] == pytest.approx(1 / (1.4 * 1.0015))
    assert f2.iloc[1] == pytest.approx(1.0)


# ── #1 默认路径：未设开关时仍走 AD，逐值等于 AD 缓存 ──
def test_p1a_default_source_is_ad(monkeypatch) -> None:
    monkeypatch.delenv(adj._FACTOR_SOURCE_ENV, raising=False)
    got = adj.load_backward_factor("300308")
    want = pd.read_csv(AD_CSV, encoding="utf-8-sig", parse_dates=["date"]
                       ).set_index("date")["factor"].astype(float)
    assert got.index.equals(want.index)
    assert (got.values - want.values).__abs__().max() == 0.0, "默认路径必须与 AD 缓存逐值一致"


# ── #6 缓存隔离：QMT 取数不得改动 AD 缓存 ──
def test_p1a_qmt_fetch_does_not_touch_ad_cache(tmp_path, monkeypatch) -> None:
    before = hashlib.sha256(AD_CSV.read_bytes()).hexdigest()
    fake = types.ModuleType("xtquant")
    fake.xtdata = types.SimpleNamespace(get_divid_factors=lambda code: pd.DataFrame({
        "time": [pd.Timestamp("2026-10-12").value // 10**6],
        "interest": [1.2], "stockBonus": [0.0], "stockGift": [0.0],
        "allotNum": [0.0], "allotPrice": [0.0], "gugai": [0.0], "dr": [1.001537]}))
    monkeypatch.setitem(sys.modules, "xtquant", fake)
    monkeypatch.setattr(adj, "_factor_qmt_file", lambda code: tmp_path / f"factor_qmt_{code}.csv")
    s = adj._fetch_factor_qmt("300308")
    assert len(s) > 8000 and abs(float(s.iloc[-1]) - 1.0) < 1e-12
    assert hashlib.sha256(AD_CSV.read_bytes()).hexdigest() == before, "AD 缓存被污染"


# ── #5 无事件标的：因子恒 1.0 + WARN，不报错 ──
def test_p1a_no_events_yields_ones(monkeypatch, capsys) -> None:
    fake = types.ModuleType("xtquant")
    fake.xtdata = types.SimpleNamespace(get_divid_factors=lambda code: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "xtquant", fake)
    monkeypatch.setattr(adj, "_WARNED", set())
    s = adj._fetch_factor_qmt("300308")
    assert (s.values == 1.0).all(), "无事件必须恒 1.0"
    assert "未返回复权事件" in capsys.readouterr().out, "必须留 WARN"


# ── #4 回退：QMT 取数失败必须回落 AD，绝不降级为"不复权" ──
def test_p1a_qmt_failure_falls_back_to_ad(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(adj, "_factor_qmt_file", lambda code: tmp_path / "nope.csv")
    monkeypatch.setattr(adj, "_WARNED", set())
    monkeypatch.setenv(adj._FACTOR_SOURCE_ENV, "qmt")

    def _boom(code):
        raise ConnectionError("miniQMT 未就绪")

    monkeypatch.setattr(adj, "_fetch_factor_qmt", _boom)
    got = adj.load_backward_factor("300308")
    want = pd.read_csv(AD_CSV, encoding="utf-8-sig", parse_dates=["date"]
                       ).set_index("date")["factor"].astype(float)
    assert (got.values - want.values).__abs__().max() < 1e-12, "必须回退 AD 口径（不能是恒 1.0）"
    assert "回退 AD 口径" in capsys.readouterr().out


# ── #2 切换路径：QMT 缓存命中时不走网络 ──
def test_p1a_qmt_source_reads_fresh_cache(tmp_path, monkeypatch) -> None:
    today = pd.Timestamp.now().normalize()
    idx = pd.DatetimeIndex([today - pd.Timedelta(days=2), today - pd.Timedelta(days=1), today])
    cache = tmp_path / "factor_qmt_300308.csv"
    pd.DataFrame({"date": idx, "factor": [1.02, 1.01, 1.0]}).to_csv(
        cache, index=False, encoding="utf-8-sig")
    monkeypatch.setattr(adj, "_factor_qmt_file", lambda code: cache)
    monkeypatch.setattr(adj, "_fetch_factor_qmt",
                        lambda code: pytest.fail("不应触发网络取数"))
    got = adj.load_backward_factor("300308", source="qmt")
    assert list(got.values) == [1.02, 1.01, 1.0]
