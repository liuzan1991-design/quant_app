# -*- coding: utf-8 -*-
"""stockdb 日频展示字段的测试。

钉住三件事：
  ① 整数 date 必须按位解析（否则 1970 年，且不报错）；
  ② 市值单位元 → 亿元；
  ③ 源不可用时 enrich 必须**原样返回**（不新增列、不崩），这是看板能否安全降级的底线。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from core import stockdb_daily as sd  # noqa: E402


def _raw(dates=(20261022, 20261023)):
    return pd.DataFrame({
        "date": list(dates),
        "code": "300308",
        "turnover": [2.5, 3.1],
        "total_mv": [2.17e11, 2.20e11],
        "float_mv": [2.17e11, 2.20e11],
        "pe_ttm": [5.0, 5.1],
        "pb": [0.47, 0.48],
        "is_st": [False, False],
    })


def test_integer_date_parsed_by_digits() -> None:
    out = sd.normalize_daily_fields(_raw())
    assert [str(d) for d in out[sd.DAY_COL]] == ["2026-10-22", "2026-10-23"]


def test_market_value_converted_to_yi() -> None:
    out = sd.normalize_daily_fields(_raw())
    assert out["总市值(亿)"].iloc[0] == pytest.approx(2170.0, abs=1e-6)
    assert out["流通市值(亿)"].iloc[0] == pytest.approx(2170.0, abs=1e-6)


def test_fields_mapped_with_chinese_names() -> None:
    out = sd.normalize_daily_fields(_raw())
    for col in ("换手率%", "总市值(亿)", "流通市值(亿)", "市盈率TTM", "市净率", "是否ST"):
        assert col in out.columns, f"缺列 {col}"
    assert out["换手率%"].iloc[1] == pytest.approx(3.1)
    # 布尔转成「是/否」，避免表格里出现 <NA>
    assert out["是否ST"].iloc[0] == "否"


def test_st_flag_shows_yes() -> None:
    raw = _raw()
    raw.loc[0, "is_st"] = True
    out = sd.normalize_daily_fields(raw)
    assert out["是否ST"].iloc[0] == "是"


def test_missing_optional_columns_are_skipped() -> None:
    raw = _raw().drop(columns=["pe_ttm", "pb", "is_st"])
    out = sd.normalize_daily_fields(raw)
    assert "市盈率TTM" not in out.columns
    assert "换手率%" in out.columns


def test_empty_input_returns_empty() -> None:
    assert sd.normalize_daily_fields(pd.DataFrame()).empty
    assert sd.normalize_daily_fields(None).empty


def _table():
    return pd.DataFrame({
        "日期/时间": ["20261022", "20261023", "20261026"],
        "代码": ["300308"] * 3,
        "收盘价": [610.0, 600.0, 595.0],
    })


def test_enrich_joins_by_day() -> None:
    fields = sd.normalize_daily_fields(_raw())
    out = sd.enrich(_table(), fields, "日期/时间")
    assert len(out) == 3
    assert out["换手率%"].iloc[0] == pytest.approx(2.5)
    # 2026-10-26 源里没有 ⇒ 留空，不填充
    assert pd.isna(out["换手率%"].iloc[2])


def test_enrich_intraday_shares_daily_value() -> None:
    """分时口径：同一天的每一根 bar 都拿到同一组日频值。"""
    t = pd.DataFrame({
        "日期/时间": ["20261023 09:30", "20261023 09:31", "20261024 09:30"],
        "代码": ["300308"] * 3,
    })
    fields = sd.normalize_daily_fields(_raw())
    out = sd.enrich(t, fields, "日期/时间")
    assert out["换手率%"].iloc[0] == pytest.approx(3.1)
    assert out["换手率%"].iloc[1] == pytest.approx(3.1)
    assert pd.isna(out["换手率%"].iloc[2])


def test_enrich_none_returns_table_unchanged() -> None:
    """源不可用 ⇒ 列结构一行不变（看板降级的底线）。"""
    t = _table()
    out = sd.enrich(t, None, "日期/时间")
    assert list(out.columns) == list(t.columns)
    assert out.equals(t)


def test_enrich_empty_fields_returns_unchanged() -> None:
    t = _table()
    out = sd.enrich(t, pd.DataFrame(), "日期/时间")
    assert list(out.columns) == list(t.columns)


def test_coverage_note_messages() -> None:
    fields = sd.normalize_daily_fields(_raw())
    assert "不可用" in sd.coverage_note(None, [])
    assert "无数据" in sd.coverage_note(pd.DataFrame(), [])
    note = sd.coverage_note(fields, ["20261022", "20261023", "20261026"])
    assert "2/3" in note
    assert "留空" in note
