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
    """把假 SDK 同时注入两个模块名 —— 适配器按"有没有 get_data"挑，两个名字都会试。"""
    def get_data(**kw):  # noqa: ANN003
        captured.update(kw)
        return payload if kw.get("as_df") else payload.to_dict("records")

    for name in ("stock_sdk", "stockdb"):
        mod = types.ModuleType(name)
        mod.rd = types.SimpleNamespace(get_data=get_data)
        monkeypatch.setitem(sys.modules, name, mod)


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


# ── SDK 路径解析（不改本机 site-packages）──────────────────────────────────
def test_pybao_dir_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(sk.PYBAO_ENV, str(tmp_path))
    assert sk.pybao_dir() == tmp_path


def test_pybao_dir_env_invalid_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(sk.PYBAO_ENV, str(tmp_path / "nope"))
    assert sk.pybao_dir() is None


def test_pybao_dir_default_missing_returns_none(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(sk.PYBAO_ENV, raising=False)
    monkeypatch.setattr(sk, "DEFAULT_PYBAO", tmp_path / "nope")
    assert sk.pybao_dir() is None


def test_import_rd_error_points_at_env(monkeypatch, tmp_path) -> None:
    """找不到 SDK 时，报错必须告诉人怎么修（而不是只说"没装"）。"""
    monkeypatch.setattr(sk, "DEFAULT_PYBAO", tmp_path / "nope")
    monkeypatch.delenv(sk.PYBAO_ENV, raising=False)
    monkeypatch.setitem(sys.modules, "stockdb", None)   # 强制 import 失败
    monkeypatch.setitem(sys.modules, "stock_sdk", None)
    with pytest.raises(sk.StockdbError, match=sk.PYBAO_ENV):
        sk._import_rd()


def test_import_rd_rejects_object_without_get_data(monkeypatch, tmp_path) -> None:
    """2026-10-25 实测踩过的坑：`stockdb.rd` 是底层 KV 客户端，**没有 get_data**。

    若只按模块名挑，会拿到一个"看起来像客户端、但调 get_data 就 AttributeError"的对象；
    这种错要到取数时才炸，比 import 失败难查得多 ⇒ 必须以 get_data 为判据。
    """
    monkeypatch.delenv(sk.PYBAO_ENV, raising=False)
    monkeypatch.setattr(sk, "DEFAULT_PYBAO", tmp_path / "nope")
    for name in ("stock_sdk", "stockdb"):
        mod = types.ModuleType(name)
        mod.rd = types.SimpleNamespace(get=1, vals=1, set=1)   # 只有底层方法
        monkeypatch.setitem(sys.modules, name, mod)
    with pytest.raises(sk.StockdbError, match="get_data"):
        sk._import_rd()


@pytest.mark.skipif(not sk.DEFAULT_PYBAO.is_dir(), reason="本机未装 stockdb")
def test_import_rd_loads_real_binding() -> None:
    """真实绑定能加载（本机已装 D:\\stockdb）。只验有 get_data，不调用、不依赖服务启动。"""
    rd = sk._import_rd()
    assert hasattr(rd, "get_data"), f"rd 不像 stockdb 查询客户端：{type(rd)}"


# ── 实测暴露的两个坑（2026-10-25 装库后才发现）──────────────────────────
def test_parse_int_date_is_not_epoch() -> None:
    """stockdb 的 `date` 是**整数**：直接 to_datetime 会当 epoch 纳秒解成 1970 年。

    这是静默错日期（不报错、日期全变成 1970），比 import 失败危险得多。
    """
    s = pd.Series([20260105, 20260803093000])
    out = sk._parse_stockdb_time(s)
    assert out.iloc[0] == pd.Timestamp("2026-01-05")
    assert out.iloc[1] == pd.Timestamp("2026-08-03 09:30:00")


def _day_241() -> pd.DataFrame:
    """构造 stockdb 的真实 241 根形态：09:30–11:29 + 13:00–14:59（240）+ 15:00 收盘竞价。"""
    times = ([pd.Timestamp("2026-08-03 09:30") + pd.Timedelta(minutes=i) for i in range(120)]
             + [pd.Timestamp("2026-08-03 13:00") + pd.Timedelta(minutes=i) for i in range(120)])
    rows = [{"date": int(t.strftime("%Y%m%d%H%M%S")), "open": 10.0, "high": 10.5,
             "low": 9.5, "close": 10.0, "volume": 100.0, "amount": 1000.0} for t in times]
    rows[-2] = {**rows[-2], "volume": 100.0}          # 14:58
    rows[-1] = {**rows[-1], "close": 10.1, "volume": 100.0}   # 14:59
    rows.append({"date": 20260803150000, "open": 10.2, "high": 10.2, "low": 10.2,
                 "close": 10.2, "volume": 500.0, "amount": 5100.0})   # 15:00 竞价
    return pd.DataFrame(rows)


def test_realign_tail_241_to_240(monkeypatch) -> None:
    """尾盘必须对齐成 AD 的 240 根：14:57 并入 14:56、15:00 改标 14:59、补两根零量。

    依据实测（2026-08-03 300308 逐根核对）：AD 14:56 带上 14:57 段的量并留两根零量，
    收盘竞价标 14:59；stockdb 把这些拆成 5 根（14:56/14:57/14:58/14:59/15:00）。
    """
    monkeypatch.setenv("STOCKDB_CALIBRATED", "1")
    out = sk.normalize_stockdb_1m(_day_241())

    assert len(out) == 240, f"应对齐成 240 根，实际 {len(out)}"
    hm = out["time"].dt.strftime("%H:%M").tolist()
    assert hm[-1] == "14:59", hm[-5:]
    assert "15:00" not in hm

    tail = out.set_index(out["time"].dt.strftime("%H:%M"))
    assert tail.loc["14:57", "volume"] == 0
    assert tail.loc["14:58", "volume"] == 0
    assert tail.loc["14:59", "volume"] == 500, "收盘竞价应落在 14:59"
    assert tail.loc["14:56", "volume"] == 200, "14:57 的量应并入 14:56（100+100）"


def test_realign_skips_day_without_1500(monkeypatch) -> None:
    """没有 15:00 的日子（数据缺失/非交易日）原样放过，交给 gaps 去报，不臆测。"""
    monkeypatch.setenv("STOCKDB_CALIBRATED", "1")
    raw = _day_241().iloc[:-1]          # 去掉 15:00 ⇒ 240 根
    out = sk.normalize_stockdb_1m(raw)
    assert len(out) == 240
    assert out["time"].dt.strftime("%H:%M").iloc[-1] == "14:59"
