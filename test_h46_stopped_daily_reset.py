# -*- coding: utf-8 -*-
"""H46-①：网格 stopped 必须「只锁当日」，不得一次触发终身停用。

背景（2026-10-15 实证）：
  `GridSharedState.stopped` 原实现只有置 True（core/grid_shared.py:135），
  全代码无任何置回 False 的路径 ⇒ 10-15 跌破硬止损线后网格永久不再买入，
  而 orders/fills 停在 1/1、心跳与水位全部正常 ⇒ 三个交易日无人发现。

本文件锁死两条语义：
  1. 跨日必须复位（Day1 触发 ⇒ Day2 首根 stopped=False）
  2. 同日仍触线必须立即重新置 True（复位不能过头）

场景全部在内存构造，不读不写任何生产数据。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backtest_generic as bg  # noqa: E402
from core.grid_shared import GridSharedEngine  # noqa: E402


def _params() -> dict:
    """trend_ma=2 让 defense 在建仓次日即为 False，避免趋势防御干扰 stopped 判定。"""
    return {
        "base_position": 100,
        "trade_shares": 100,
        "grid_pct": 2.0,
        "grid_levels": 5,
        "max_position": 6000,
        "center_freeze_pct": 8.0,
        "hard_stop_pct": 12.0,
        "max_grid_loss_pct": 5.0,
        "trend_ma": 2,
        "enable_afternoon_close": False,
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "stamp_tax_rate": 0.0,
        "transfer_fee_rate": 0.0,
        "slippage_bps": 0,
        "_init_cash": 1_000_000.0,
    }


def _bar(day: str, hhmm: str, close: float, vol: float = 1000.0) -> dict:
    return {
        "time": f"{day} {hhmm}:00",
        "close": close,
        "amount": close * vol,
        "volume": vol,
        "signal_close": close,
    }


def _setup():
    params = _params()
    engine = GridSharedEngine(dict(params))
    account = bg.SimAccount(1_000_000.0, dict(params))
    return engine, account


def test_stopped_fires_when_price_breaks_hard_stop_line():
    """基准：建仓价 100、floor=92、止损线 80.96；close 80 必须把 stopped 置真。"""
    engine, account = _setup()
    engine.on_bar(_bar("2026-10-15", "09:30", 100.0), account)
    assert engine.state.stopped is False
    engine.on_bar(_bar("2026-10-15", "09:31", 80.0), account)
    assert engine.state.stopped is True
    # 止损线 = max(VWAP, anchor*0.92) * (1-12%) = 92 * 0.88
    assert engine.state.base_price == pytest.approx(92.0, abs=1e-9)


def test_stopped_resets_on_next_trading_day():
    """H46-① 核心：Day1 触发后，Day2 首根必须复位为 False。"""
    engine, account = _setup()
    engine.on_bar(_bar("2026-10-15", "09:30", 100.0), account)
    engine.on_bar(_bar("2026-10-15", "09:31", 80.0), account)
    assert engine.state.stopped is True, "前置：Day1 应已触发硬止损"

    engine.on_bar(_bar("2026-10-16", "09:30", 100.0), account)
    assert engine.state.stopped is False, "跨日后 stopped 必须复位（修复前恒为 True）"


def test_stopped_can_trade_again_after_reset():
    """复位后若价格进入买入档，必须真的产生 BUY（不能只是标志位变了）。"""
    engine, account = _setup()
    engine.on_bar(_bar("2026-10-15", "09:30", 100.0), account)
    engine.on_bar(_bar("2026-10-15", "09:31", 80.0), account)
    assert engine.state.stopped is True

    engine.on_bar(_bar("2026-10-16", "09:30", 100.0), account)  # 复位
    assert engine._trend_defense_active() is False, "前置：本场景 defense 应为 False"
    out = engine.on_bar(_bar("2026-10-16", "09:31", 95.0), account) or []
    buys = [t for t in out if t.get("direction") == "BUY" and not t.get("rejected")]
    assert buys, "stopped 复位且 defense=False 时，跌破买入档应恢复买入"
    assert engine.state.grid_bought == 1


def test_stopped_rearmed_if_still_below_line():
    """复位不能过头：次日开盘仍在止损线下方 ⇒ 必须立即重新置 True。"""
    engine, account = _setup()
    engine.on_bar(_bar("2026-10-15", "09:30", 100.0), account)
    engine.on_bar(_bar("2026-10-15", "09:31", 80.0), account)
    assert engine.state.stopped is True

    # Day2 开盘 80，仍 <= 92*0.88=80.96
    engine.on_bar(_bar("2026-10-16", "09:30", 80.0), account)
    assert engine.state.stopped is True, "次日仍触线应重新置 True，不能被复位放过"


def test_stopped_does_not_leak_into_defense():
    """两把锁必须独立：not stopped and not defense 是两个条件，不能互相覆盖。"""
    engine, account = _setup()
    engine.on_bar(_bar("2026-10-15", "09:30", 100.0), account)
    engine.on_bar(_bar("2026-10-15", "09:31", 80.0), account)
    engine.on_bar(_bar("2026-10-16", "09:30", 100.0), account)
    # 复位后 stopped=False，但 defense 仍是它自己的计算值，不能被 stopped 复位影响
    assert engine.state.stopped is False
    assert isinstance(engine._trend_defense_active(), bool)


# ---------------------------------------------------------------------------
# H46-③：摘要必须落出四个状态字段（否则外部仍读不出「已停摆」）
# ---------------------------------------------------------------------------

def test_summary_writes_grid_state_fields(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import live_match_loop
    from core.grid_shared import GridSharedState

    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})
    st = GridSharedState(base_price=772.8, anchor_price=840.0,
                         grid_bought=0, stopped=True)
    obs = live_match_loop.Observer(
        code="300308",
        strategy_id="grid_trade",
        symbol="300308.SZ",
        params={},
        broker=SimpleNamespace(
            account=SimpleNamespace(equity=lambda: 981_814.0, cash=916_087.0),
            orders={}, fills=[]),
        store=SimpleNamespace(root=tmp_path),
    )
    obs.engine = SimpleNamespace(state=st, _trend_defense_active=lambda: True)
    obs.equity_high = 1_000_000.0
    obs.day_start_equity = 982_157.0
    obs.day_high_equity = 982_157.0
    obs.max_drawdown_cumulative = 0.0

    payload = live_match_loop._write_daily_summary(obs)

    assert payload["stopped"] is True
    assert payload["defense"] is True
    assert payload["grid_bought"] == 0
    assert payload["base_price"] == pytest.approx(772.8, abs=1e-9)
    # 必须真的落盘（daily_summary.json 供下游脚本读取）
    import json
    on_disk = json.loads((tmp_path / "daily_summary.json").read_text(encoding="utf-8"))
    assert on_disk["stopped"] is True
    assert on_disk["base_price"] == pytest.approx(772.8, abs=1e-9)


def test_summary_omits_grid_state_for_ma_swing(tmp_path, monkeypatch):
    """ma_swing 没有网格状态，不得凭空造字段（避免下游误读）。"""
    from types import SimpleNamespace

    import live_match_loop

    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})
    obs = live_match_loop.Observer(
        code="300308",
        strategy_id="ma_swing",
        symbol="300308.SZ",
        params={},
        broker=SimpleNamespace(
            account=SimpleNamespace(equity=lambda: 1_000_000.0, cash=1_000_000.0),
            orders={}, fills=[]),
        store=SimpleNamespace(root=tmp_path),
    )
    obs.engine = SimpleNamespace(state=None)
    obs.equity_high = 1_000_000.0
    obs.day_start_equity = 1_000_000.0
    obs.day_high_equity = 1_000_000.0
    obs.max_drawdown_cumulative = 0.0

    payload = live_match_loop._write_daily_summary(obs)
    for k in ("stopped", "defense", "grid_bought", "base_price"):
        assert k not in payload, f"ma_swing 摘要不应包含 {k}"


# ---------------------------------------------------------------------------
# H46-③ 层级陷阱（2026-10-20 收盘后实测暴露）：
#   生产 obs.engine = GridSharedLiveEngine，state / _trend_defense_active
#   都在**内层** self.engine（GridSharedEngine）上；LiveEngine 自身两者皆无。
#   上面那条测试用的是**单层** SimpleNamespace 替身 ⇒ 测不出这个差异，
#   曾导致四字段在生产上静默缺失（无报错、无告警）。
# ---------------------------------------------------------------------------

def test_summary_writes_state_fields_for_nested_live_engine(tmp_path, monkeypatch):
    """双层结构（对齐生产 GridSharedLiveEngine）也必须写出四字段。"""
    from types import SimpleNamespace

    import live_match_loop
    from core.grid_shared import GridSharedState

    monkeypatch.setattr(live_match_loop, "_compare_signals", lambda obs: {})
    st = GridSharedState(base_price=772.8, anchor_price=840.0,
                         grid_bought=0, stopped=True)
    inner = SimpleNamespace(state=st, _trend_defense_active=lambda: True)
    shell = SimpleNamespace(engine=inner)          # 形状对齐 GridSharedLiveEngine

    obs = live_match_loop.Observer(
        code="300308",
        strategy_id="grid_trade",
        symbol="300308.SZ",
        params={},
        broker=SimpleNamespace(
            account=SimpleNamespace(equity=lambda: 980_877.0, cash=916_087.0),
            orders={}, fills=[]),
        store=SimpleNamespace(root=tmp_path),
    )
    obs.engine = shell
    obs.equity_high = 1_000_000.0
    obs.day_start_equity = 982_157.0
    obs.day_high_equity = 982_157.0
    obs.max_drawdown_cumulative = 0.0

    payload = live_match_loop._write_daily_summary(obs)

    assert payload["stopped"] is True
    assert payload["defense"] is True         # 必须取到**内层**的方法
    assert payload["grid_bought"] == 0
    assert payload["base_price"] == pytest.approx(772.8, abs=1e-9)


def test_grid_live_engine_has_no_own_state_attribute():
    """结构哨兵：LiveEngine 自身无 state，取法必须往内层走。

    若哪天 GridSharedLiveEngine 自己加了 state，本测试会失败，
    提醒复核 live_match_loop 的层级取法（而不是悄悄按旧假设继续）。
    """
    from core.grid_shared_live import GridSharedLiveEngine

    assert "state" not in dir(GridSharedLiveEngine)
    assert "_trend_defense_active" not in dir(GridSharedLiveEngine)
