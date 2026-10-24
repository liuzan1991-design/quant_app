# -*- coding: utf-8 -*-
"""H38：交易日历判定 + 门禁收敛（唯一入口）。

三类断言：
  1. 判定正确性：周末/节假日/覆盖区外（未来日历未发布）分别怎么判；
  2. 时区：本机 GMT+3 → 北京日期换算（不能想当然用 `datetime.now().date()`）；
  3. **防漂移**：`live_run.py` / `watchdog.py` 不得再各自写 `weekday() >= 5`，
     必须都从 `core.trading_calendar` 取判定（登记要求"收敛到一处"）。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from core import trading_calendar as tc

APP = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """隔离真实缓存/网络：每个用例用独立内存态。"""
    monkeypatch.setattr(tc, "_CACHE", {
        date(2026, 9, 24): True,     # 周四，交易日
        date(2026, 9, 25): False,    # 中秋（周五），休市  ← H38 原始触发日
        date(2026, 10, 1): False,    # 国庆
        date(2026, 10, 9): True,     # 周五
        date(2026, 10, 23): True,    # 周五（日历覆盖上界）
    }, raising=True)
    monkeypatch.setattr(tc, "_COVERAGE_END", date(2026, 10, 23), raising=True)
    monkeypatch.setattr(tc, "_WARNED", set(), raising=True)
    monkeypatch.setattr(tc, "_save_cache", lambda: None, raising=True)
    monkeypatch.setattr(tc, "_fetch_year", lambda y: False, raising=True)


def test_h38_holiday_is_not_trading_day() -> None:
    assert tc.is_trading_day(date(2026, 9, 25)) is False, "中秋周五必须判休市（H38 原始场景）"
    assert tc.is_trading_day(date(2026, 10, 1)) is False


def test_h38_normal_trading_day() -> None:
    assert tc.is_trading_day(date(2026, 10, 9)) is True
    assert tc.is_trading_day(date(2026, 10, 23)) is True


def test_h38_beyond_coverage_degrades_not_holiday(capsys) -> None:
    """🚨 未来日历未发布：**不能判休市**，只能降级为周一~周五并告警。

    QMT `get_trading_dates` 只返回到"最后一根已知交易日"（实测 2026 年只到 10-23），
    若把"不在返回列表"当成休市，下周一会被误判成节假日、整条采集被跳过。
    """
    assert tc.is_trading_day(date(2026, 10, 26)) is True, "覆盖区外的周一本应可运行（降级判定）"
    assert "未覆盖" in capsys.readouterr().out
    assert tc.is_trading_day(date(2026, 10, 31)) is False, "覆盖区外的周六仍应判休市"


def test_h38_beijing_date_conversion() -> None:
    # 本机 GMT+3：本地 04:30 = 北京 09:30（同一天）
    assert tc.beijing_today(datetime(2026, 10, 23, 4, 30)) == date(2026, 10, 23)
    # 本地 21:00 = 北京次日 02:00 ⇒ 日期翻页（若用本地日期会错判）
    assert tc.beijing_today(datetime(2026, 10, 23, 21, 0)) == date(2026, 10, 24)


@pytest.mark.parametrize("fname", ["live_run.py", "watchdog.py"])
def test_h38_no_independent_weekday_gate(fname: str) -> None:
    """登记要求：周末/节假日判定**收敛到一处**，禁止第三处独立判定。"""
    raw = (APP / fname).read_text(encoding="utf-8")
    # 只扫**代码行**：注释里出现"原先自判 weekday()"属说明文字，不是判定逻辑
    code = "\n".join(ln for ln in raw.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "weekday() >= 5" not in code, f"{fname} 仍在自判周末 ⇒ 判定漂移风险"
    assert "trading_calendar" in raw, f"{fname} 未复用统一交易日历判定"


@pytest.mark.parametrize("fname", ["live_run.py", "watchdog.py"])
def test_h38_gate_actually_called(fname: str) -> None:
    """机制存在 ≠ 被调用：必须真的调用 `is_trading_day`，不只是 import。"""
    src = (APP / fname).read_text(encoding="utf-8")
    assert "is_trading_day(" in src, f"{fname} 导入了判定却没有调用"
