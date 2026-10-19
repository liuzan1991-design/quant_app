# -*- coding: utf-8 -*-
"""H45 回归：复权因子对齐不得因「窗口起点晚于因子末日」而降级。

背景：原实现 `factor.reindex(dates).ffill()` 先把索引裁成 dates，结果集里不再包含
因子末日那一行；当窗口全部晚于因子末日（live 每轮只喂当日窗口）时整列 NaN，
ffill 无源可填 ⇒ 抛异常 ⇒ 静默降级为不复权。观察期 20/20 全部踩中。
"""
from __future__ import annotations

import pandas as pd
import pytest

from core import adjustment


@pytest.fixture(autouse=True)
def _reset_module_state():
    adjustment._REFRESH_ATTEMPTED.clear()
    adjustment._WARNED.clear()
    yield
    adjustment._REFRESH_ATTEMPTED.clear()
    adjustment._WARNED.clear()


def _write_factor(dirpath, rows):
    pd.DataFrame({"date": [d for d, _ in rows],
                  "factor": [v for _, v in rows]}).to_csv(
        dirpath / "factor_300308.csv", index=False, encoding="utf-8-sig")


def _bars(days, price):
    idx = [pd.Timestamp(f"{d} {m}") for d in days for m in ("09:30", "10:00", "14:59")]
    return pd.DataFrame({"time": idx, "open": price, "high": price,
                         "low": price, "close": price, "volume": 100})


def test_window_after_factor_end_keeps_adjustment(tmp_path, monkeypatch):
    """窗口全部晚于因子末日时仍须走前复权口径（H45 根因）。"""
    _write_factor(tmp_path, [("2026-08-26", 6.603442),
                             ("2026-08-27", 6.603442),
                             ("2026-08-28", 6.603442)])
    monkeypatch.setattr(adjustment, "DATA_DIR", tmp_path)
    monkeypatch.setattr(adjustment, "_STALE_MAX_LAG_DAYS", 10_000)  # 本用例只验证对齐

    out, events, mode = adjustment.prepare_signal_prices(
        _bars(["2026-09-09", "2026-09-10", "2026-09-11"], 800.0), "300308")

    assert mode == "真实价成交 + 前复权信号"
    assert out["signal_close"].tolist() == out["close"].tolist()
    assert out.attrs["corporate_action_dates"] == []


def test_cross_ex_rights_window_scales_pre_close(tmp_path, monkeypatch):
    """跨除权窗口：除权前信号价按因子比缩放，除权日不再缩放。"""
    _write_factor(tmp_path, [("2026-10-08", 6.603442),
                             ("2026-10-09", 6.603442),
                             ("2026-10-12", 6.613593)])
    monkeypatch.setattr(adjustment, "DATA_DIR", tmp_path)
    monkeypatch.setattr(adjustment, "_STALE_MAX_LAG_DAYS", 10_000)

    out, events, mode = adjustment.prepare_signal_prices(
        _bars(["2026-10-09", "2026-10-12"], 800.0), "300308")

    assert mode == "真实价成交 + 前复权信号"
    assert events == [pd.Timestamp("2026-10-12").date()]
    ratio = 6.603442 / 6.613593
    pre = out[out["time"] < pd.Timestamp("2026-10-12")]["signal_close"]
    post = out[out["time"] >= pd.Timestamp("2026-10-12")]["signal_close"]
    assert all(abs(float(v) - 800.0 * ratio) < 1e-9 for v in pre)
    assert all(abs(float(v) - 800.0) < 1e-9 for v in post)


def test_stale_cache_falls_back_and_refreshes_once(tmp_path, monkeypatch):
    """缓存过期且刷新失败时回退旧缓存，且同一进程当日只尝试一次（防重登）。"""
    _write_factor(tmp_path, [("2026-08-26", 6.603442)])
    monkeypatch.setattr(adjustment, "DATA_DIR", tmp_path)
    calls = {"n": 0}

    def boom(code):
        calls["n"] += 1
        raise RuntimeError("AD 查询通道不可用")

    monkeypatch.setattr(adjustment, "_fetch_factor", boom)

    first = adjustment.load_backward_factor("300308")
    second = adjustment.load_backward_factor("300308")

    assert len(first) == 1 and len(second) == 1
    assert float(second.iloc[-1]) == pytest.approx(6.603442)
    assert calls["n"] == 1, "过期刷新必须节流，否则 live 每轮都会登录 AD"


def test_fresh_cache_never_refreshes(tmp_path, monkeypatch):
    """缓存新鲜时不得触发任何取数。"""
    today = pd.Timestamp.now().normalize().strftime("%Y-%m-%d")
    _write_factor(tmp_path, [(today, 6.6)])
    monkeypatch.setattr(adjustment, "DATA_DIR", tmp_path)
    monkeypatch.setattr(adjustment, "_fetch_factor",
                        lambda code: pytest.fail("新鲜缓存不应刷新"))

    series = adjustment.load_backward_factor("300308")

    assert float(series.iloc[-1]) == pytest.approx(6.6)


def test_no_cache_and_fetch_fails_raises(tmp_path, monkeypatch):
    """既无缓存又取数失败时必须抛异常，由上层显式降级而非静默返回空。"""
    monkeypatch.setattr(adjustment, "DATA_DIR", tmp_path)
    monkeypatch.setattr(adjustment, "_fetch_factor",
                        lambda code: (_ for _ in ()).throw(RuntimeError("无缓存")))

    with pytest.raises(RuntimeError):
        adjustment.load_backward_factor("300308")
