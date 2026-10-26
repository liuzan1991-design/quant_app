# -*- coding: utf-8 -*-
"""tool_market_screen_db 的纯函数测试。

重点钉住两条曾经真出过事的口径：
  ① stockdb 的 date 是**整数**，pd.to_datetime(整数) 会按 epoch 纳秒解成 1970 年且不报错；
  ② RE = 日均振幅 / 日均|净涨跌|，分子分母的口径必须与 tool_market_screen.py 一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from scripts import tool_market_screen_db as ms  # noqa: E402


def test_integer_date_8digits_parsed_as_yyyymmdd() -> None:
    """8 位整数必须按 YYYYMMDD 解析 —— 直接 pd.to_datetime 会变成 1970 年。"""
    raw = pd.DataFrame({"date": [20261021, 20261022, 20261023],
                        "close": [10.0, 10.5, 11.0]})
    out = ms.normalize_daily(raw)
    assert out["date"].dt.year.tolist() == [2026, 2026, 2026]
    assert out["date"].iloc[-1].strftime("%Y%m%d") == "20261023"


def test_integer_date_14digits_parsed_as_datetime() -> None:
    raw = pd.DataFrame({"date": [20261023093000, 20261023093100], "close": [10.0, 10.1]})
    out = ms.normalize_daily(raw)
    assert out["date"].iloc[0].strftime("%Y%m%d%H%M%S") == "20261023093000"


def test_string_date_with_dash() -> None:
    raw = pd.DataFrame({"date": ["2026-10-22", "2026-10-23"], "close": [10.0, 10.2]})
    out = ms.normalize_daily(raw)
    assert out["date"].iloc[1].strftime("%Y-%m-%d") == "2026-10-23"


def test_empty_returns_empty_frame() -> None:
    assert ms.normalize_daily(pd.DataFrame()).empty


def _synth(n=130, up=1.005, band=0.02, amount=1e8):
    """构造：日内振幅恒 band、日净涨跌恒 (up-1) ⇒ RE 应恒为 band/(up-1)。"""
    close = [10.0]
    for _ in range(n - 1):
        close.append(close[-1] * up)
    prev = [close[0]] + close[:-1]
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": close,
        "high": [p * (1 + band / 2) for p in prev],
        "low": [p * (1 - band / 2) for p in prev],
        "close": close,
        "volume": [1e6] * n,
        "amount": [amount] * n,
    })
    return df


def test_re_matches_definition() -> None:
    """振幅 2% / 净涨跌 0.5% ⇒ RE = 4.00。"""
    m = ms.compute_metrics(_synth(), "300308", "测试")
    assert m is not None
    assert m["amp_pct"] == pytest.approx(2.0, abs=1e-6)
    assert m["net_pct"] == pytest.approx(0.5, abs=1e-6)
    assert m["RE"] == pytest.approx(4.0, abs=1e-3)
    assert m["days"] == 130
    assert m["avg_amount_yi"] == pytest.approx(1.0, abs=1e-9)


def test_re_high_when_net_move_zero() -> None:
    """净涨跌为 0（横盘）⇒ RE 应为无穷大，不得除零崩溃。"""
    n = 130
    prev = [10.0] * n
    df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": prev,
        "high": [10.2] * n,
        "low": [9.8] * n,
        "close": prev,
        "amount": [1e8] * n,
    })
    m = ms.compute_metrics(df, "000001", "")
    assert m is not None
    assert m["RE"] == float("inf")


def test_too_few_days_returns_none() -> None:
    assert ms.compute_metrics(_synth(n=60), "300308", "") is None


def test_missing_amount_does_not_crash() -> None:
    df = _synth().drop(columns=["amount"])
    m = ms.compute_metrics(df, "300308", "")
    assert m is not None
    assert m["avg_amount_yi"] != m["avg_amount_yi"]  # NaN
