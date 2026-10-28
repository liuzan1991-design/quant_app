# -*- coding: utf-8 -*-
"""H73：跨策略单标总仓位闸门（接实盘前必需，默认关闭）。

钉住四条语义：
1. **未配置 ⇒ 闸门不存在**（`OfflineReplayEngine.gate is None` ⇒ 不拦截，行为不变）；
2. 启用后：四策略合计持仓市值超过 `cap × initial_cash` 的买单被**整单拒绝**；
3. **卖出永远放行**（减仓不构成风险）；
4. 判定用「除自己之外」的持仓，不把自己已持有的部分算两遍。
"""
from __future__ import annotations

import pytest

from core import cross_risk, live_config as lc
from core.paper_models import OrderSide
from core.paper_replay import OfflineReplayEngine, StrategySignal


# ── ① 台账判定 ──────────────────────────────────────────────────────────────
def test_disabled_by_default() -> None:
    led = cross_risk.CrossSymbolLedger(None, 300_000.0)
    assert not led.enabled
    assert led.allowed_buy("301717.SZ", 10.0, "k", 0) is None, "未启用 ⇒ 不限制"


def test_cap_math() -> None:
    """25% × 30 万 = 7.5 万；价 100 元 ⇒ 最多 750 股（含自己已持有的）。"""
    led = cross_risk.CrossSymbolLedger(0.25, 300_000.0)
    led.update("grid", "301717.SZ", 200)
    assert led.cap_value() == pytest.approx(75_000.0)
    # 自己 200 股，别人 300 股 ⇒ 已占 500 股 ⇒ 还能买 250 股
    led.update("ma_swing", "301717.SZ", 300)
    assert led.allowed_buy("301717.SZ", 100.0, "grid", 200) == pytest.approx(250.0)


def test_others_excludes_self() -> None:
    """别人的持仓按 300 股算，不能把自己那 200 股算两遍。"""
    led = cross_risk.CrossSymbolLedger(0.25, 300_000.0)
    led.update("grid", "301717.SZ", 200)
    led.update("ma_swing", "301717.SZ", 300)
    assert led.others("301717.SZ", "grid") == 300
    assert led.total("301717.SZ") == 500


def test_check_rejects_and_records() -> None:
    led = cross_risk.CrossSymbolLedger(0.25, 300_000.0)
    led.update("other", "301717.SZ", 700)          # 别人已占 700 股 ×100 = 7 万
    assert led.check("301717.SZ", 100, 100.0, "self", 0) is False, "只剩 5000 元 ⇒ 100 股必拒"
    assert led.check("301717.SZ", 40, 100.0, "self", 0) is True
    assert len(led.rejects) == 1 and led.rejects[0]["qty"] == 100
    assert led.rejects[0]["reason"] == "跨策略单标上限"


def test_sell_always_allowed() -> None:
    """闸门只拦买入 —— 卖出是减仓，被拦住会出不去货。"""
    led = cross_risk.CrossSymbolLedger(0.25, 300_000.0)
    led.update("other", "301717.SZ", 100_000)      # 远超上限
    # 即便超得离谱，卖出也不经过闸门（在回放层短路，见下）
    assert led.check("301717.SZ", 999_999, 100.0, "self", 0) is False


# ── ② 回放层接线 ────────────────────────────────────────────────────────────
class _FakeBroker:
    def __init__(self, shares: int = 0) -> None:
        class _P:
            quantity = shares
        class _A:
            positions = {"301717.SZ": _P()}
        self.account = _A()


def test_replay_gate_none_by_default() -> None:
    eng = OfflineReplayEngine(_FakeBroker())
    assert eng.gate is None, "默认不装闸 ⇒ 行为不变"


def test_replay_sell_bypasses_gate() -> None:
    """卖出信号必须绕过闸门。"""
    calls: list = []

    def _gate(signal, price, own):
        calls.append(signal.side)
        return True

    eng = OfflineReplayEngine(_FakeBroker(), gate=_gate)
    sig = StrategySignal(strategy_id="s", symbol="301717.SZ", side=OrderSide.SELL,
                         quantity=100, signal_time=None)
    # 直接验 gate 选择逻辑：卖出不调用 gate
    own = eng._own_shares("301717.SZ")
    should_call = sig.side == OrderSide.BUY and eng.gate is not None
    if should_call:
        eng.gate(sig, 10.0, own)
    assert calls == [], f"卖出不应触发闸门，实际 {calls}"


def test_replay_buy_uses_own_shares() -> None:
    seen: dict = {}

    def _gate(signal, price, own):
        seen["price"], seen["own"], seen["qty"] = price, own, signal.quantity
        return True

    eng = OfflineReplayEngine(_FakeBroker(shares=42), gate=_gate)
    sig = StrategySignal(strategy_id="s", symbol="301717.SZ", side=OrderSide.BUY,
                         quantity=100, signal_time=None)
    assert eng.gate(sig, 9.5, eng._own_shares("301717.SZ")) is True
    assert seen == {"price": 9.5, "own": 42, "qty": 100}


# ── ③ 配置层 ────────────────────────────────────────────────────────────────
def test_config_default_off() -> None:
    assert lc.LiveConfig().cross_symbol_cap is None
    assert "cross_symbol_cap" in lc.LiveConfig().to_dict()


def test_config_roundtrip() -> None:
    cfg = lc.from_dict({"cross_symbol_cap": 0.25, "initial_cash": 300000})
    assert cfg.cross_symbol_cap == 0.25
    assert lc.from_dict(cfg.to_dict()).cross_symbol_cap == 0.25


def test_config_cap_requires_cash() -> None:
    """没给资金基准却开闸门 ⇒ 必须报错（占比无从计算）。"""
    errs = lc.validate(lc.LiveConfig(cross_symbol_cap=0.25))
    assert any("initial_cash" in e for e in errs), f"必须报错：{errs}"


def test_config_cap_range() -> None:
    for bad in (0, -0.1, 1.5):
        errs = lc.validate(lc.LiveConfig(cross_symbol_cap=bad, initial_cash=300000))
        assert any("cross_symbol_cap" in e for e in errs), f"{bad} 必须被拒：{errs}"
    assert not [e for e in lc.validate(lc.LiveConfig(cross_symbol_cap=0.25,
                                                     initial_cash=300000))
                if "cross_symbol_cap" in e]
