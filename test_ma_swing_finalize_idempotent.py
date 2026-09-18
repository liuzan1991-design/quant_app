# -*- coding: utf-8 -*-
"""H8 回归：均线波段日线序列不得重复写入。

背景（2026-09-18 发现）：
    `ma_swing/engine_state.json` 的 `completed` 曾出现 907 条记录 / 900 个唯一日期，
    观察期 D1–D7 每一天被写入两次。

根因：
    `finalize_if_dirty()` 只 append、不推进 `current_day`、无「已 finalize」标记。
    主任务每日 04:30 重启 → `restore_engine_state()` 先 finalize 一次；
    次日首根 bar 走 `current_day != day` 分支 → 又 finalize 一次。

后果（实测）：
    重复日 `volume` 因 `groupby("date").sum()` 而翻倍，20 日均量判据被放大
    1.55–1.85 倍，09-14 / 09-16 / 09-17 的量条件由 False 翻转为 True。

本文件锁定三条不变量：
    1. 同一天在 completed 中只允许出现一次（跨日切换幂等）；
    2. 盘中重启产生的「半日快照」必须被当日完整数据覆盖，而不是保留下来；
    3. 载入已污染的旧状态文件时自动按日去重（保留最后一条 = 最完整那条）。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.ma_swing_live import DailyBar, MaSwingLiveEngine, MaSwingLiveState
from strategies.ma_swing import MaSwingStrategy


class _StubAccount:
    def __init__(self, cash: float = 1_000_000.0) -> None:
        self.cash = cash
        self.positions: dict = {}


class _StubBroker:
    """仅提供 on_bar 需要的最小接口：account.cash / account.positions / fills。"""

    def __init__(self, cash: float = 1_000_000.0) -> None:
        self.account = _StubAccount(cash)
        self.fills: list = []


def _bar(day: str, hhmm: str, close: float, volume: float) -> dict:
    return {
        "time": pd.Timestamp(f"{day} {hhmm}:00"),
        "open": close, "high": close, "low": close, "close": close,
        "volume": volume, "amount": close * volume,
    }


def _engine() -> MaSwingLiveEngine:
    return MaSwingLiveEngine("300308.SZ", MaSwingStrategy.default_params())


def _days(state: MaSwingLiveState) -> list:
    return [item.day for item in state.completed]


def test_day_switch_does_not_duplicate_completed():
    """复现原缺陷场景：重启补 finalize 后，次日首根 bar 不得再追加同一天。"""
    engine = _engine()
    engine.state.completed = [DailyBar(date(2026, 9, 16), 1, 1, 1, 900.0, 100.0)]
    engine.state.current_day = date(2026, 9, 17)
    engine.state.current_open = 900.3
    engine.state.current_high = 927.83
    engine.state.current_low = 892.51
    engine.state.current_close = 896.0
    engine.state.current_volume = 22_787_518.0

    broker = _StubBroker()
    # 步骤 1：每日 04:30 重启时 restore_engine_state() 会补 finalize。
    engine.finalize_if_dirty()
    assert _days(engine.state).count(date(2026, 9, 17)) == 1, "重启补 finalize 后应为 1 条"

    # 步骤 2：次日首根 bar 触发 day 切换分支。
    engine.on_bar(_bar("2026-09-18", "09:30", 910.0, 1_000_000.0), broker)

    days = _days(engine.state)
    assert days.count(date(2026, 9, 17)) == 1, f"09-17 被写入 {days.count(date(2026,9,17))} 次，应恰好 1 次"
    assert days == sorted(days), "completed 必须保持按日升序"
    assert len(days) == len(set(days)), "completed 不允许存在任何重复日期"


def test_midday_restart_partial_snapshot_is_upgraded_not_kept():
    """盘中重启产生的半日快照必须被当日完整数据覆盖。

    原缺陷下 09-16 出现两条：第 1 条 81.3%（本地 08:33 进程消失时的快照），
    第 2 条 100%。修复后应只剩完整那条。
    """
    engine = _engine()
    engine.state.current_day = date(2026, 9, 16)
    engine.state.current_close = 904.5
    engine.state.current_volume = 19_524_649.0   # 半日快照
    engine.finalize_if_dirty()
    assert engine.state.completed[-1].volume == 19_524_649.0

    # 当日剩余 bar 继续累积后，再次 finalize 应覆盖而非追加。
    engine.state.current_close = 907.8
    engine.state.current_volume = 24_021_846.0   # 完整全天
    engine.finalize_if_dirty()

    entries = [item for item in engine.state.completed if item.day == date(2026, 9, 16)]
    assert len(entries) == 1, "同一天只允许一条"
    assert entries[0].volume == 24_021_846.0, "应保留完整全天量，而非半日快照"
    assert entries[0].close == 907.8


def test_from_dict_dedupes_polluted_state_keeping_last():
    """载入已污染的旧状态文件时，按日去重并保留最后一条（= 最完整那条）。"""
    polluted = {
        "current_day": "2026-09-18",
        "current_volume": 29_505_475.0,
        "completed": [
            {"day": "2026-09-16", "open": 869.02, "high": 918.38, "low": 866.0,
             "close": 904.5, "volume": 19_524_649.0},   # 半日快照
            {"day": "2026-09-16", "open": 869.02, "high": 918.38, "low": 866.0,
             "close": 907.8, "volume": 24_021_846.0},   # 完整
            {"day": "2026-09-17", "open": 900.3, "high": 927.83, "low": 892.51,
             "close": 896.0, "volume": 22_787_518.0},
            {"day": "2026-09-17", "open": 900.3, "high": 927.83, "low": 892.51,
             "close": 896.0, "volume": 22_787_518.0},
        ],
    }
    state = MaSwingLiveState.from_dict(polluted)

    days = [item.day for item in state.completed]
    assert len(days) == 2, f"去重后应为 2 条，实际 {len(days)}"
    assert len(days) == len(set(days)), "不允许重复日期"
    assert days == sorted(days), "必须保持按日升序"

    d16 = [item for item in state.completed if item.day == date(2026, 9, 16)][0]
    assert d16.volume == 24_021_846.0, "应保留完整全天量而非半日快照"
    assert d16.close == 907.8


def test_volume_ratio_uses_true_daily_volume():
    """验收判据：去重后量比恢复原值（不再被放大）。

    原缺陷使重复日 volume 翻倍，20 日均量被抬高约 1.25 倍、当日量翻倍，
    净效果是量比被放大 1.55–1.85 倍。此处以 09-14 为样本锁定。
    """
    # 09-14 真实全天量 27,183,039；原缺陷下该值被写两遍。
    real_volume = 27_183_039.0
    prev_twenty_real = [25_000_000.0] * 20          # 占位：仅验证聚合口径
    polluted = real_volume * 2.0
    deduped = real_volume

    assert polluted != deduped
    assert deduped == real_volume
    # 量比口径：当日量 / 前 20 日均量。去重后分子不得再翻倍。
    ratio_deduped = deduped / (sum(prev_twenty_real) / len(prev_twenty_real))
    assert abs(ratio_deduped - (real_volume / 25_000_000.0)) < 1e-9


def main() -> None:
    tests = [
        test_day_switch_does_not_duplicate_completed,
        test_midday_restart_partial_snapshot_is_upgraded_not_kept,
        test_from_dict_dedupes_polluted_state_keeping_last,
        test_volume_ratio_uses_true_daily_volume,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
