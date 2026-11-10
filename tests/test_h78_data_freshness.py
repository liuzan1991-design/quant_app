# -*- coding: utf-8 -*-
"""H78 回归：行情新鲜度必须按"最后一根已完成 bar vs 应有水位"判，不按总根数。

背景（2026-11-10）：09:56 断连后数据停在 09:54，账本权益用 14.41 的旧 mark
报出 +0.31%，对外呈"在赚钱"。当天总根数 25 根、缺口 89%，但**没有任何字段
说明"这是旧价格"**。

本组用例锁五件事（用户 2026-11-10 审阅口径）：
1. 滞后（lag）与历史缺口（gap）分开算：总根数够但最后一根老 ⇒ 仍判陈旧；
2. 午休/节假日/开盘前不得误报；
3. 停牌状态未知时**不得**当正常，只能报"需人工确认"；
4. 心跳/摘要字段缺失时读取方按"未知"处理；
5. 界面陈旧时必须标红，且当日盈亏不得照常呈现。
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import pytest

from core import data_freshness as fresh


def _dt(h, m, day=10):
    return datetime(2026, 11, day, h, m)


# ── 一、滞后 vs 缺口 ─────────────────────────────────────────────────────────

def test_lag_not_total_count():
    """240 根齐全但最后一根停在 15 分钟前 ⇒ 陈旧（总根数会骗人）。"""
    r = fresh.assess(_dt(14, 5), _dt(13, 50), 240)
    assert r["stale"] is True
    assert r["lag_bars"] == 14         # 14:04 应为最后一根，实际 13:50
    assert r["gap_bars"] == 0          # 历史无缺口


def test_historical_gap_alone_is_not_stale():
    """中间缺过 bar（历史缺口）但最后一根是新的 ⇒ 不算陈旧，缺口单独记录。"""
    r = fresh.assess(_dt(15, 10), _dt(14, 59), 230)
    assert r["stale"] is False
    assert r["gap_bars"] == 10
    assert r["lag_bars"] == 0


def test_lag_and_historical_gap_are_separate():
    """尾部落后 14 根 + 中间缺 6 根：三个数必须各归各位，不能合并成一个缺口。"""
    r = fresh.assess(_dt(14, 5), _dt(13, 50), 165)
    assert r["lag_bars"] == 14
    assert r["gap_bars"] == 6          # 13:50 之前应 171 根，实有 165
    assert r["missing_bars"] == 20     # 185 - 165


def test_boundary_nine_bars_ok_ten_bars_stale():
    """阈值 10 根（2026-11-10 实测正常滞后最大 8 根）：9 根不报，10 根报。"""
    assert fresh.assess(_dt(14, 5), _dt(13, 55), 240)["lag_bars"] == 9
    assert fresh.assess(_dt(14, 5), _dt(13, 55), 240)["stale"] is False
    assert fresh.assess(_dt(14, 5), _dt(13, 54), 240)["stale"] is True


# ── 二、午休 / 节假日 / 开盘前 ──────────────────────────────────────────────

def test_lunch_frozen_watermark_no_false_alarm():
    r = fresh.assess(_dt(12, 0), _dt(11, 29), 120)
    assert r["session"] == "lunch"
    assert r["stale"] is False
    assert "午休" in r["reason"]


def test_lunch_reveals_gap_before_break():
    """午休不是"免检"：11:10 就停了，12:00 仍必须报陈旧。"""
    r = fresh.assess(_dt(12, 0), _dt(11, 10), 101)
    assert r["stale"] is True
    assert r["lag_bars"] == 19


def test_pre_open_never_stale():
    r = fresh.assess(_dt(9, 10), None, 0)
    assert r["session"] == "pre_open"
    assert r["stale"] is False


def test_non_trading_day_never_stale():
    r = fresh.assess(datetime(2026, 11, 7, 14, 0), datetime(2026, 11, 6, 14, 59), 240)
    assert r["session"] == "non_trading_day"
    assert r["stale"] is False


def test_post_close_incomplete_is_stale():
    """收盘后水位没到 14:59 ⇒ 仍算陈旧，不能因为"收盘了"就当正常。"""
    r = fresh.assess(_dt(15, 10), _dt(14, 49), 230)
    assert r["stale"] is True
    assert r["lag_bars"] == 10
    assert r["gap_bars"] == 0        # 历史无缺口（缺的 10 根全在尾部）
    assert r["missing_bars"] == 10


# ── 三、停牌 ────────────────────────────────────────────────────────────────

def test_suspension_unknown_is_never_normal():
    r = fresh.assess(_dt(14, 5), _dt(13, 0), 120, suspension="unknown")
    assert r["stale"] is True
    assert "停牌未知" in r["reason"]
    assert r["suspension"] == "unknown"


def test_suspension_confirmed_still_flagged_but_labelled():
    r = fresh.assess(_dt(14, 5), None, 0, suspension="confirmed")
    assert r["stale"] is True
    assert "停牌" in r["reason"]


# ── 四、CSV 扫描 ────────────────────────────────────────────────────────────

def test_scan_day_bars_last_and_count(tmp_path):
    p = tmp_path / "stock_300319_1m.csv"
    p.write_text(
        "time,open,high,low,close,volume,amount\n"
        "2026-11-09 14:59:00,1,1,1,1,1,1\n"
        "2026-11-10 09:30:00,1,1,1,1,1,1\n"
        "2026-11-10 09:31:00,1,1,1,1,1,1\n", encoding="utf-8")
    last, n = fresh.scan_day_bars(p, "2026-11-10")
    assert n == 2 and last == datetime(2026, 11, 10, 9, 31)


def test_scan_missing_file_is_unknown_not_zero(tmp_path):
    last, n = fresh.scan_day_bars(tmp_path / "nope.csv", "2026-11-10")
    assert last is None and n == 0
    # 0 根 + 盘中 ⇒ 陈旧；不得因"没有文件"判正常
    assert fresh.assess(_dt(10, 0), None, 0)["stale"] is True


def test_worst_of_picks_most_lagged():
    a = fresh.assess(_dt(14, 5), _dt(14, 4), 240)
    b = fresh.assess(_dt(14, 5), _dt(13, 50), 240)
    assert fresh.worst_of([a, b])["lag_bars"] == b["lag_bars"]


# ── 五、落盘字段与界面 ─────────────────────────────────────────────────────

def test_heartbeat_carries_stale_fields(tmp_path):
    import live_run
    p = tmp_path / "heartbeat.json"
    fr = fresh.assess(_dt(14, 5), _dt(13, 50), 230)
    live_run._write_heartbeat(p, _dt(9, 30), "running", _dt(14, 0), 0, 0, freshness=fr)
    hb = json.loads(p.read_text(encoding="utf-8"))
    assert hb["stale"] is True
    assert hb["data_stale_seconds"] == fr["data_stale_seconds"]
    assert hb["data_freshness"]["lag_bars"] == fr["lag_bars"]


def test_heartbeat_without_freshness_has_no_fake_normal(tmp_path):
    """算不出来就不写字段——读取方必须落"未知"，不能默认成正常。"""
    import live_run
    p = tmp_path / "heartbeat.json"
    live_run._write_heartbeat(p, _dt(9, 30), "starting", None, 0, 0, freshness=None)
    hb = json.loads(p.read_text(encoding="utf-8"))
    assert "stale" not in hb


def test_ui_marks_stale_and_blanks_day_pnl(tmp_path):
    from ui import live_tab
    status = {"units": [{
        "symbol": "300319.SZ", "strategy": "grid_trade",
        "summary": {"date": "2026-11-10", "equity": 300929.3, "cash": 264904.3,
                    "day_pnl": 929.3, "cum_pnl": 929.3, "stale": True,
                    "data_freshness": {"last_bar_at": "2026-11-10T09:54:00",
                                       "lag_bars": 226},
                    "data_quality": {"status": "void", "gap_pct": 0.9}},
        "state": {}, "watermark": {}, "history": [],
    }]}
    df = live_tab.snapshot_table(status, today="2026-11-10")
    assert df.loc[0, "新鲜度"] == "⚠ 陈旧"
    assert pd.isna(df.loc[0, "当日盈亏"])          # 旧价格的盈亏不得呈现
    assert df.loc[0, "行情时点"] == "2026-11-10T09:54:00"


def test_ui_unknown_stale_is_not_shown_as_normal(tmp_path):
    """摘要停留在上一交易日 ⇒ 必须标"非当日"，不能因为字段缺失就显示正常。"""
    from ui import live_tab
    status = {"units": [{
        "symbol": "300319.SZ", "strategy": "ma_swing",
        "summary": {"date": "2026-11-09", "equity": 300000.0},
        "state": {}, "watermark": {}, "history": [],
    }]}
    df = live_tab.snapshot_table(status, today="2026-11-10")
    assert df.loc[0, "新鲜度"] == "⚠ 非当日"
    assert pd.isna(df.loc[0, "当日盈亏"])
