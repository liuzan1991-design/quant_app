# -*- coding: utf-8 -*-
"""「数据看板」页的测试（视觉参照 free-stockdb，逻辑归我们自己）。

重点：**聚合口径不能错**。
日K 的 open 取首根、close 取末根、volume 求和，一旦取错（比如把 1m 的 close
当成日 close），看板上看着没错、回测里却全是偏差。所以这里逐字段钉住。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ui import data_board as db

APP = Path(__file__).resolve().parent


def _bars(day: str = "2026-10-22", n: int = 6, start_h: int = 9, start_m: int = 30) -> pd.DataFrame:
    t0 = pd.Timestamp(f"{day} {start_h:02d}:{start_m:02d}")
    rows = []
    for i in range(n):
        rows.append({"time": t0 + pd.Timedelta(minutes=i),
                     "open": 10.0 + i, "high": 10.5 + i, "low": 9.5 + i,
                     "close": 10.2 + i, "volume": 100.0 + i, "amount": 1000.0 + i})
    return pd.DataFrame(rows)


# ── 重采样 ──────────────────────────────────────────────────────────────────
def test_resample_1min_is_passthrough() -> None:
    df = _bars(n=6)
    assert db.resample_bars(df, "1min").equals(df)


def test_resample_5min_aggregates_within_day() -> None:
    out = db.resample_bars(_bars(n=6), "5min")
    assert len(out) == 2, f"6 根 1m 应合成 2 根 5m，实际 {len(out)}"
    first = out.iloc[0]
    assert first["open"] == 10.0, "open 应取首根"
    assert first["close"] == 14.2, "close 应取末根(第5根 10.2+4)"
    assert first["high"] == 14.5 and first["low"] == 9.5
    assert first["volume"] == sum(100.0 + i for i in range(5))


def test_resample_never_crosses_overnight() -> None:
    """A 股不能跨日聚合：两天各 2 根 1m，做 5m 必须仍是 2 根（每天 1 根）。"""
    df = pd.concat([_bars("2026-10-22", n=2), _bars("2026-10-23", n=2)], ignore_index=True)
    out = db.resample_bars(df, "5min")
    assert len(out) == 2, f"跨日被合并了，实际 {out}"
    assert out["time"].dt.strftime("%Y-%m-%d").nunique() == 2


def test_resample_daily_one_bar_per_day() -> None:
    df = pd.concat([_bars("2026-10-22", n=4), _bars("2026-10-23", n=4)], ignore_index=True)
    out = db.resample_bars(df, "1d")
    assert len(out) == 2
    d = out.iloc[0]
    assert d["open"] == 10.0 and d["close"] == 13.2
    assert d["high"] == 13.5 and d["low"] == 9.5


def test_resample_empty_is_safe() -> None:
    assert db.resample_bars(pd.DataFrame(columns=["time", "open", "high", "low",
                                                 "close", "volume", "amount"]), "5min").empty


# ── 列与派生字段 ────────────────────────────────────────────────────────────
def test_board_frame_columns_follow_reference_order() -> None:
    out = db.board_frame(_bars(n=3), "300308", "1min")
    assert list(out.columns) == ["日期/时间", "代码", "名称", "开盘价", "最高价", "最低价",
                                 "收盘价", "前收盘价", "成交量", "成交额", "涨幅%", "振幅%",
                                 "量比"]


def test_prev_close_and_pct() -> None:
    out = db.board_frame(_bars(n=3), "300308", "1min")
    # 第 1 根：前收 = 自己的 open（不引入 NaN，避免首行涨幅空）
    assert out.iloc[0]["前收盘价"] == 10.0
    # 第 2 根：前收 = 第 1 根 close = 10.2；涨幅 = (11.2/10.2 - 1)%
    assert out.iloc[1]["前收盘价"] == 10.2
    assert out.iloc[1]["涨幅%"] == pytest.approx(9.8)  # 11.2/10.2-1 = 9.8039% → 展示 2 位

def test_amplitude_uses_prev_close() -> None:
    out = db.board_frame(_bars(n=3), "300308", "1min")
    # 第 2 根：high 11.5, low 10.5, 前收 10.2 ⇒ 振幅 = 1.0/10.2 = 9.8039% → 展示 2 位
    assert out.iloc[1]["振幅%"] == pytest.approx(9.8)


def test_volume_ratio_only_for_daily() -> None:
    intraday = db.board_frame(_bars(n=3), "300308", "1min")
    assert intraday["量比"].isna().all(), "分时口径不装样子，量比必须为空"

    days = pd.concat([_bars(f"2026-10-{d:02d}", n=2) for d in (20, 21, 22)],
                     ignore_index=True)
    daily = db.board_frame(db.resample_bars(days, "1d"), "300308", "1d")
    assert daily["量比"].notna().any()


def test_board_frame_empty_is_safe() -> None:
    assert db.board_frame(pd.DataFrame(), "300308", "1d").empty


# ── 数据定位 ────────────────────────────────────────────────────────────────
def test_local_csv_prefers_live_pool() -> None:
    p = db.local_csv("300308")
    assert p is not None and "live" in str(p.parent), f"应优先 live 池，实际 {p}"


def test_local_csv_missing_returns_none() -> None:
    assert db.local_csv("999999") is None


def test_load_local_real_file() -> None:
    df = db.load_local("300308")
    assert {"time", "open", "high", "low", "close", "volume"} <= set(df.columns)
    assert df["time"].is_monotonic_increasing


# ── 整页冒烟（慢档：AppTest 起整个 app）─────────────────────────────────────
@pytest.mark.slow
def test_app_has_board_tab_and_no_exception() -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP / "app.py"), default_timeout=300)
    at.run()
    assert len(at.exception) == 0, [str(e.value)[:200] for e in at.exception]
    tabs = [t.label for t in at.tabs] if hasattr(at, "tabs") else []
    if tabs:
        assert any("数据看板" in t for t in tabs), tabs
    labels = [t.label for t in at.text_input]
    assert "代码" in labels, f"看板筛选条缺少「代码」输入：{labels}"


@pytest.mark.slow
def test_click_read_button_renders_table() -> None:
    """交互照搬参考页：点「读取数据」才出表。这里验证点了确实出表（不是只有按钮）。"""
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP / "app.py"), default_timeout=300)
    at.run()
    assert len(at.exception) == 0, [str(e.value)[:200] for e in at.exception]
    # 其他 tab 本身已有 dataframe，所以不能断言"零表格"，只能断言"点完之后多出看板那张"
    before = len(at.dataframe)

    btn = [b for b in at.button if (b.label or "") == "读取数据"]
    assert btn, f"找不到「读取数据」按钮：{[b.label for b in at.button]}"
    btn[0].click().run()
    assert len(at.exception) == 0, [str(e.value)[:200] for e in at.exception]
    assert len(at.dataframe) > before, "点「读取数据」后应多出看板表格"

    boards = [d.value for d in at.dataframe
              if isinstance(getattr(d, "value", None), pd.DataFrame)
              and "开盘价" in getattr(d.value, "columns", [])]
    assert boards, "看板表格缺少参考页的列（开盘价…）"
    assert "涨幅%" in boards[-1].columns and len(boards[-1]) > 0
