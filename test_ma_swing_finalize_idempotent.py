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
from core.ma_swing_signals import build_daily_signal_table
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
    """验收判据：去重后量比恢复原值，且 `breakout_entry` 的量条件不得被误翻为 True。

    ⚠️ 本用例为**真实回归测试**，走完整生产链路：
        污染状态字典 → `MaSwingLiveState.from_dict()`（读取侧去重）
        → `engine._daily_frame()`（生产取数）→ `build_daily_signal_table()`（生产判据）
        → 读 `breakout_entry`。

    首版此用例用占位数字 (`[25_000_000.0] * 20`) 自行算了一遍除法再断言相等，
    属**同义反复**，不具备防回归能力（由外部审查指出，2026-09-18 重写）。

    H8 机制：重复日 `volume` 经 `groupby("date").sum()` 精确 ×2；20 日均量因
    窗口内仅部分日重复而抬高约 1.25~1.3 倍（净效果量比放大 1.55~1.85 倍）。
    下方用 25 天缓涨序列 + 末日放量突破价格前高，**隔离出量条件**，
    使量比是否被放大直接决定 `breakout_entry` 真值。
    """
    params = MaSwingStrategy.default_params()
    n_days = 25
    # 价格缓涨：末日 close=124 > 前 20 日最高 123 ⇒ 价格条件成立，只剩量条件可翻转
    rows = []
    for i in range(n_days):
        close = 100.0 + i
        rows.append({"day": f"2026-08-{i + 1:02d}", "open": close, "high": close,
                     "low": close, "close": close, "volume": 1_000_000.0})

    # 污染形态：复刻 H8 实际形态（观察期最后 7 天被写两遍）
    polluted_rows = rows + rows[-7:]
    assert len(polluted_rows) == 32 and len({r["day"] for r in polluted_rows}) == 25

    # —— 走生产链路：from_dict 去重 → _daily_frame → 生产判据 ——
    state = MaSwingLiveState.from_dict({
        "current_day": "2026-08-26", "current_volume": 0.0, "completed": polluted_rows,
    })
    assert len(state.completed) == n_days, "读取侧应去重为 25 条"

    engine = _engine()
    engine.state = state
    daily = engine._daily_frame().reset_index().rename(columns={"day": "time"})
    # ⚠️ 不能只看 daily["volume"].iloc[-1]——`_daily_frame()` 是**未聚合的原始帧**，
    # 重复条目不会改变末行取值，那样写等于假断言（首版即踩此坑，靠变异测试发现）。
    # 真正有判别力的是下面两条：源帧日期唯一 + 按生产过程聚合后的当日量。
    assert daily["time"].is_unique, "生产取数帧不允许存在重复日期"
    aggregated = daily.groupby(daily["time"])["volume"].sum()
    assert aggregated.iloc[-1] == 1_000_000.0, \
        f"聚合后当日量不得被重复写入放大，实际 {aggregated.iloc[-1]:,.0f}"

    signals = build_daily_signal_table(daily, params)
    avg_volume = aggregated.rolling(20, min_periods=20).mean().shift(1).iloc[-1]
    ratio = aggregated.iloc[-1] / avg_volume
    assert abs(ratio - 1.0) < 1e-9, f"去重后量比应恰为 1.0，实际 {ratio:.3f}"
    assert not bool(signals["breakout_entry"].iloc[-1]), \
        "量比 1.0 < 门槛 1.3，量条件必须为 False"

    # —— 对照：若重复未被去掉（即 H8 未修），量比被放大到 ~1.54，量条件被误翻为 True ——
    # 生产判据要求 time 列，故此处把 day 改名为 time（与 _daily_frame().reset_index() 一致）
    polluted_daily = pd.DataFrame(polluted_rows).rename(columns={"day": "time"})
    polluted_agg = (polluted_daily.assign(d=pd.to_datetime(polluted_daily["time"]))
                    .groupby("d")["volume"].sum())
    polluted_avg = polluted_agg.rolling(20, min_periods=20).mean().shift(1).iloc[-1]
    polluted_ratio = polluted_agg.iloc[-1] / polluted_avg
    polluted_signals = build_daily_signal_table(polluted_daily, params)
    assert polluted_ratio > 1.5, f"污染量比应被放大到 1.5 以上，实际 {polluted_ratio:.3f}"
    assert bool(polluted_signals["breakout_entry"].iloc[-1]), \
        "污染下量条件应被误翻为 True（本对照证明该用例确有防回归能力）"


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
