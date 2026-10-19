# -*- coding: utf-8 -*-
"""H38-B / H35 回归：收盘判定必须校验日期归属，硬收盘不得嵌在 bar 条件里。

**H38-B（假收盘）**：旧判据只比 `latest.time() >= 14:59`，不看日期。
上一交易日 14:59 的末根会让"今天"立刻满足收盘 ⇒ 非交易日空跑（2026-09-25
中秋）、开盘前中断重启（2026-09-30）都被写成"今日已收盘"的摘要。

**H35（兜底不可达）**：旧代码把 `_past_hard_close()` 写在
`if latest.time() >= 14:59:` 内部 ⇒ 采集全天失败、latest 停在今日 14:30 时
外层条件不成立，硬收盘永远走不到 ⇒ 摘要 / health 全不写。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from core.close_timing import MARKET_CLOSE_TIME, is_today_close_bar

TODAY = date(2026, 10, 19)      # D24（周一）
PREV = date(2026, 10, 16)       # 上一交易日（周五）
APP_DIR = Path(__file__).resolve().parent


def test_none_is_not_close():
    """本轮没读到数据 → 不是收盘信号（否则空数据会被当收盘）。"""
    assert is_today_close_bar(None, TODAY) is False


@pytest.mark.parametrize("hh,mm", [(9, 30), (11, 29), (13, 0), (14, 30), (14, 58)])
def test_today_before_close_is_not_close(hh, mm):
    assert is_today_close_bar(datetime(TODAY.year, TODAY.month, TODAY.day, hh, mm),
                              TODAY) is False


@pytest.mark.parametrize("hh,mm", [(14, 59), (15, 0)])
def test_today_at_or_after_close_is_close(hh, mm):
    """`>=` 而非 `==`：极端数据会给 15:00 标签，不能因此漏收盘。"""
    assert is_today_close_bar(datetime(TODAY.year, TODAY.month, TODAY.day, hh, mm),
                              TODAY) is True


@pytest.mark.parametrize("hh,mm", [(14, 59), (15, 0)])
def test_previous_day_bar_is_not_close(hh, mm):
    """**H38-B 核心**：昨日末根长得像收盘，但不是今天的。"""
    assert is_today_close_bar(datetime(PREV.year, PREV.month, PREV.day, hh, mm),
                              TODAY) is False


def test_default_today_falls_back_to_clock():
    now = datetime.now()
    assert is_today_close_bar(datetime(now.year, now.month, now.day, 14, 59)) is True


def test_market_close_constant_is_1459():
    """数据实测：无 15:00 标签，末根标 14:59（若改口径须同步改这里）。"""
    assert MARKET_CLOSE_TIME == datetime.strptime("14:59", "%H:%M").time()


def test_hard_close_is_independent_branch():
    """**H35 结构防回归**：硬收盘必须是独立分支，排在 bar 判定之前。

    只要有人把 `_past_hard_close()` 挪回 `if is_today_close_bar(...)` 内部，
    兜底通道就会重新失效（采集失败时 latest 永远到不了 14:59）。
    """
    src = (APP_DIR / "live_run.py").read_text(encoding="utf-8")
    i_hard = src.index("if _past_hard_close():")
    i_bar = src.index("elif is_today_close_bar(latest):")
    assert i_hard < i_bar, "硬收盘兜底必须在 bar 判定之前独立成支（H35）"
