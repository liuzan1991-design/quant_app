# -*- coding: utf-8 -*-
"""H58：free-stockdb 数据源分支的测试。

核心不是"能不能取到数"，而是**三条口径换算对不对**、**没校准会不会被拦住**。
前者错 = 策略拿到错 K 线（H53/H54 同类事故）；后者漏 = 未校准数据静默进 live。
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from core import data_fetch, stockdb_kline as sk

STD = ["time", "open", "high", "low", "close", "volume", "amount"]


def _raw(n_days: int = 2, bars_per_day: int = 240) -> pd.DataFrame:
    """构造 stockdb 形态的原始 1m（日期列叫 date，末根含 15:00）。"""
    rows = []
    for d in range(n_days):
        day = pd.Timestamp("2026-10-22") + pd.Timedelta(days=d)
        for i in range(bars_per_day):
            t = day + pd.Timedelta(hours=9, minutes=30) + pd.Timedelta(minutes=i)
            rows.append({"date": t, "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": 100.5, "volume": 12.0, "amount": 1200.0})
    return pd.DataFrame(rows)


@pytest.fixture
def cal(monkeypatch):
    monkeypatch.setenv("STOCKDB_CALIBRATED", "1")
    monkeypatch.setenv("STOCKDB_LABEL_SHIFT", "0")
    monkeypatch.setenv("STOCKDB_VOLUME_MULT", "100")
    monkeypatch.setenv("STOCKDB_FQ", "none")
    return sk.calibration()


# ── 纯函数：normalize ───────────────────────────────────────────────────────
def test_normalize_maps_to_std_cols_and_applies_calibration(monkeypatch) -> None:
    """口径换算必须由校准参数驱动：标签平移 + 成交量倍数 + 列名归一。"""
    c = {"calibrated": True, "label_shift_min": 1.0, "volume_mult": 100.0, "fq": "none"}
    out = sk.normalize_stockdb_1m(_raw(1, 3), c)
    assert list(out.columns) == STD, list(out.columns)
    # 标签平移：源 09:30 → 09:29
    assert out.iloc[0]["time"] == pd.Timestamp("2026-10-22 09:29")
    # 成交量 ×100
    assert out.iloc[0]["volume"] == 1200.0


def test_normalize_shift_zero_keeps_labels(monkeypatch) -> None:
    c = {"calibrated": True, "label_shift_min": 0.0, "volume_mult": 1.0, "fq": "none"}
    out = sk.normalize_stockdb_1m(_raw(1, 2), c)
    assert out.iloc[0]["time"] == pd.Timestamp("2026-10-22 09:30")
    assert out.iloc[0]["volume"] == 12.0


def test_normalize_dedups_and_sorts(monkeypatch) -> None:
    c = {"calibrated": True, "label_shift_min": 0.0, "volume_mult": 1.0, "fq": "none"}
    df = _raw(1, 3)
    df.loc[3] = df.loc[0]          # 重复时间戳，后出现的应保留
    df.loc[3, "close"] = 999.0
    out = sk.normalize_stockdb_1m(df.iloc[[2, 0, 1, 3]], c)
    assert len(out) == 3
    assert out.iloc[0]["close"] == 999.0, "同时间戳应保留最后一条"


def test_normalize_rejects_empty() -> None:
    c = {"calibrated": True, "label_shift_min": 0.0, "volume_mult": 1.0, "fq": "none"}
    with pytest.raises(sk.StockdbError):
        sk.normalize_stockdb_1m(pd.DataFrame(), c)


def test_normalize_rejects_missing_price_col() -> None:
    c = {"calibrated": True, "label_shift_min": 0.0, "volume_mult": 1.0, "fq": "none"}
    df = _raw(1, 2).drop(columns=["close"])
    with pytest.raises(sk.StockdbError, match="close"):
        sk.normalize_stockdb_1m(df, c)


# ── 安全闸：未校准拒绝出数 ──────────────────────────────────────────────────
def test_require_calibrated_raises_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("STOCKDB_CALIBRATED", raising=False)
    with pytest.raises(sk.StockdbNotCalibrated):
        sk.require_calibrated()


def test_fetch_refuses_before_touching_sdk(monkeypatch) -> None:
    """未校准 ⇒ 连 SDK 都不该 import（否则等于"先取数再判断"，风险已发生）。"""
    monkeypatch.delenv("STOCKDB_CALIBRATED", raising=False)
    called = {"n": 0}

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        called["n"] += 1
        raise AssertionError("不该走到 SDK")

    monkeypatch.setattr(sk, "_import_rd", _boom)
    with pytest.raises(sk.StockdbNotCalibrated):
        sk.fetch_stockdb_kline("300308", "2026-10-22", "2026-10-23")
    assert called["n"] == 0


def test_invalid_fq_rejected(monkeypatch) -> None:
    monkeypatch.setenv("STOCKDB_CALIBRATED", "1")
    monkeypatch.setenv("STOCKDB_FQ", "bogus")
    with pytest.raises(sk.StockdbError, match="STOCKDB_FQ"):
        sk.require_calibrated()


# ── 取数链路（注入假 SDK，不碰真库）─────────────────────────────────────────
def _fake_sdk(monkeypatch, payload: pd.DataFrame, captured: dict) -> None:
    def get_data(**kw):  # noqa: ANN003
        captured.update(kw)
        return payload if kw.get("as_df") else payload.to_dict("records")

    mod = types.ModuleType("stockdb")
    mod.rd = types.SimpleNamespace(get_data=get_data)
    monkeypatch.setitem(sys.modules, "stockdb", mod)


def test_fetch_uses_sdk_params_and_reports_gaps(monkeypatch, cal) -> None:
    """fq=None（live 要复权口径 none）、frequency=1m、日期转 8 位；缺根进 attrs。"""
    cap: dict = {}
    _fake_sdk(monkeypatch, _raw(2, 239), cap)      # 每天 239 根 ⇒ 应报缺口

    df = sk.fetch_stockdb_kline("300308", "2026-10-22", "2026-10-23")

    assert cap["code"] == "300308"
    assert cap["start"] == "20261022" and cap["end"] == "20261023"
    assert cap["frequency"] == "1m"
    assert cap["fq"] is None, "STOCKDB_FQ=none 必须传 None（不复权）"
    assert list(df.columns) == STD
    assert df.attrs["source"] == "stockdb"
    assert set(df.attrs["gaps"]) == {"2026-10-22", "2026-10-23"}, df.attrs["gaps"]
    assert all(v == 239 for v in df.attrs["gaps"].values())


def test_fetch_no_gaps_when_full_day(monkeypatch, cal) -> None:
    cap: dict = {}
    _fake_sdk(monkeypatch, _raw(1, 240), cap)
    df = sk.fetch_stockdb_kline("300308", "2026-10-22", "2026-10-22")
    assert df.attrs["gaps"] == {}


def test_fetch_sdk_error_wrapped(monkeypatch, cal) -> None:
    mod = types.ModuleType("stockdb")

    def _raise(**kw):  # noqa: ANN003
        raise ConnectionError("refused")

    mod.rd = types.SimpleNamespace(get_data=_raise)
    monkeypatch.setitem(sys.modules, "stockdb", mod)
    with pytest.raises(sk.StockdbError, match="refused"):
        sk.fetch_stockdb_kline("300308", "2026-10-22", "2026-10-22")


# ── 接线层：stockdb 分支失败必须回退 AD（且 AD 走假实现，不碰网络）──────────
def test_data_fetch_stockdb_falls_back_to_ad(monkeypatch) -> None:
    monkeypatch.delenv("STOCKDB_CALIBRATED", raising=False)
    sentinel = pd.DataFrame([{"time": pd.Timestamp("2026-10-22 09:30"),
                              "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                              "volume": 1.0, "amount": 1.0}])
    monkeypatch.setattr(data_fetch, "_fetch_kline_inner", lambda *a, **k: sentinel.copy())

    got = data_fetch.fetch_kline("300308", "2026-10-22", "2026-10-22",
                                 source="stockdb")
    assert got.equals(sentinel), "未校准的 stockdb 必须回退到 AD 数据"


def test_default_source_unchanged(monkeypatch) -> None:
    """默认未设环境变量时仍走 ad —— 接线不得改变现状（P1-b 同款红线）。"""
    monkeypatch.delenv("QUANT_KLINE_SOURCE", raising=False)
    sentinel = pd.DataFrame([{"time": pd.Timestamp("2026-10-22 09:30")}])
    touched = {"stockdb": 0}

    monkeypatch.setattr(data_fetch, "_fetch_kline_inner", lambda *a, **k: sentinel.copy())
    monkeypatch.setattr(data_fetch, "_fetch_kline_stockdb",
                        lambda *a, **k: touched.__setitem__("stockdb", touched["stockdb"] + 1))

    data_fetch.fetch_kline("300308", "2026-10-22", "2026-10-22")
    assert touched["stockdb"] == 0, "默认源不得碰 stockdb 分支"
