"""日终账本比对（core/ledger_audit.py）测试。

fixture 用的是 2026-11-06 300319.SZ 的真实数值（初始 30 万、2500 股 @14.037015、
费用 3.158328、期末现金 264904.304171625），不是编造的数。
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from core import ledger_audit as la


def _bar(day="2026-11-06", hm="14:39", o=14.03, h=14.04, l=14.02, c=14.04):
    return {f"{day}T{hm}": {"open": o, "high": h, "low": l, "close": c}}


def _fill(**kw):
    base = {"fill_id": "f1", "client_order_id": "c1", "symbol": "300319.SZ",
            "side": "BUY", "quantity": 2500, "price": 14.037014999999998,
            "fee": 3.1583283750000004, "filled_at": "2026-11-06T14:39:00"}
    base.update(kw)
    return base


def _state(cash=264904.304171625, qty=2500, initial=300000.0):
    return {"account": {"initial_cash": initial, "cash": cash,
                        "positions": {"300319.SZ": {"quantity": qty}}},
            "orders": [], "fills": []}


def _order(coid="c1", status="FILLED", filled=2500, created="2026-11-06T14:38:00",
           updated="2026-11-06T14:39:00"):
    return {"request": {"client_order_id": coid, "symbol": "300319.SZ",
                        "side": "BUY", "quantity": filled, "created_at": created,
                        "strategy_id": "grid_trade", "reason": "建底仓"},
            "status": status, "filled_quantity": filled,
            "updated_at": updated, "reject_reason": ""}


# ── 读数 ────────────────────────────────────────────────────────────────
def test_load_bars_handles_bom_and_space_separated_time(tmp_path: Path):
    p = tmp_path / "b.csv"
    p.write_text("\ufefftime,open,high,low,close,volume\n"
                 "2026-11-06 14:39:00,14.03,14.04,14.02,14.04,1000\n"
                 "not-a-row,x,y,z,w\n", encoding="utf-8")
    bars = la.load_bars(p)
    assert list(bars) == ["2026-11-06T14:39"]
    assert bars["2026-11-06T14:39"]["low"] == pytest.approx(14.02)


def test_find_bar_falls_back_and_reports_shift():
    bars = _bar(hm="14:58")
    bar, shift = la.find_bar(bars, "2026-11-06T14:59:00")
    assert bar is not None and shift == -1


def test_find_bar_missing_returns_none():
    assert la.find_bar({}, "2026-11-06T14:59:00") == (None, None)


# ── 成交价校验 ──────────────────────────────────────────────────────────
def test_fill_price_inside_bar_is_ok():
    out = la.check_fill_price(_fill(), _bar())
    assert out["inside"] is True and out["level"] == "ok"
    assert out["margin_lo_pct"] == pytest.approx((14.037014999999998 - 14.02) / 14.02 * 100)


def test_fill_price_outside_bar_is_blocker():
    out = la.check_fill_price(_fill(price=14.50), _bar())
    assert out["inside"] is False and out["level"] == "blocker"


def test_fill_without_bar_is_warn_not_blocker():
    """无 bar 只警告：可能是标签口径问题，不能直接判账本错。"""
    out = la.check_fill_price(_fill(), {})
    assert out["inside"] is None and out["level"] == "warn"


def test_fill_missing_price_is_blocker():
    assert la.check_fill_price(_fill(price=None), _bar())["level"] == "blocker"


# ── 算术 ────────────────────────────────────────────────────────────────
def test_recompute_cash_matches_real_state():
    out = la.recompute_cash(300000.0, [_fill()])
    assert out["expected"] == pytest.approx(264904.304171625)
    assert out["buy_amount"] == pytest.approx(35092.537499999996)
    assert out["fee"] == pytest.approx(3.1583283750000004)


def test_recompute_cash_counts_missing_fee():
    out = la.recompute_cash(1000.0, [{"side": "BUY", "quantity": 10, "price": 10.0}])
    assert out["missing_fee"] == 1


def test_recompute_position_nets_buy_and_sell():
    fills = [_fill(), _fill(side="SELL", quantity=100, price=15.0)]
    assert la.recompute_position(fills)["300319.SZ"] == 2400


# ── 整日比对 ────────────────────────────────────────────────────────────
def test_audit_day_ok_on_real_numbers():
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ",
                       signals=[{"signal_time": "2026-11-06T14:38:00",
                                 "client_order_id": "c1", "order_status": "SUBMITTED"}],
                       orders=[_order()], fills=[_fill()],
                       state=_state(), bars=_bar())
    assert rep["verdict"] == "ok"
    assert rep["cash"]["diff"] == pytest.approx(0.0, abs=1e-6)
    assert rep["blockers"] == []


def test_audit_day_detects_cash_drift():
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ", signals=[],
                       orders=[_order()], fills=[_fill()],
                       state=_state(cash=264914.30), bars=_bar())
    assert rep["verdict"] == "mismatch"
    assert any("现金不闭环" in b for b in rep["blockers"])


def test_audit_day_detects_position_drift():
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ", signals=[],
                       orders=[_order()], fills=[_fill()],
                       state=_state(qty=2400), bars=_bar())
    assert rep["verdict"] == "mismatch"
    assert any("持仓不闭环" in b for b in rep["blockers"])


def test_audit_day_detects_out_of_range_fill_price():
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ", signals=[],
                       orders=[_order()], fills=[_fill(price=20.0)],
                       state=_state(cash=300000.0 - 2500 * 20.0 - 3.16), bars=_bar())
    assert any("越出 bar" in b for b in rep["blockers"])


def test_audit_day_detects_order_fill_qty_mismatch():
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ", signals=[],
                       orders=[_order(filled=2500)], fills=[_fill(quantity=2000)],
                       state=_state(), bars=_bar())
    assert any("filled_quantity" in b for b in rep["blockers"])


def test_rejected_signal_is_not_orphan():
    """拒单在 signals 里就写 REJECTED 且无 client_order_id ⇒ 不该报"断链"。"""
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ",
                       signals=[{"signal_time": "2026-11-06T13:46:00",
                                 "client_order_id": "", "order_status": "REJECTED"}],
                       orders=[], fills=[], state=_state(cash=300000.0, qty=0), bars={})
    assert rep["orders"]["rejects"] == 1
    assert rep["orders"]["orphan_signals"] == []
    assert not any("拟下单" in w for w in rep["warnings"])


def test_submitted_signal_without_order_is_warning():
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ",
                       signals=[{"signal_time": "2026-11-06T09:40:00",
                                 "client_order_id": "zzz", "order_status": "SUBMITTED"}],
                       orders=[], fills=[], state=_state(cash=300000.0, qty=0), bars={})
    assert any("拟下单" in w for w in rep["warnings"])


def test_previous_day_order_not_counted_today():
    rep = la.audit_day(day="2026-11-07", symbol="300319.SZ", signals=[],
                       orders=[_order()], fills=[_fill()], state=_state(), bars=_bar())
    assert rep["counts"]["orders"] == 0 and rep["counts"]["fills"] == 0


def test_corporate_action_downgrades_cash_gap_to_warning():
    """300308 真实账本：初始 100 万、买入 100 股 @839.029305、账面 916207。

    算术应为 916,087.00，差 +120.00；该账本记过 2026-10-12 的公司行为
    ⇒ 降级为「须按事件核对」，不得判阻断（否则每次分红都会误杀）。
    """
    fill = _fill(symbol="300308.SZ", quantity=100, price=839.029305,
                 fee=10.068351660000001, filled_at="2026-09-07T09:30:00")
    state = {"account": {"initial_cash": 1000000.0, "cash": 916207.0,
                         "positions": {"300308.SZ": {"quantity": 100}},
                         "applied_corporate_actions": {"300308.SZ": "2026-10-12"}}}
    rep = la.audit_day(day="2026-09-07", symbol="300308.SZ", signals=[],
                       orders=[], fills=[fill], state=state, bars={})
    assert rep["cash"]["diff"] == pytest.approx(120.0, abs=0.01)   # 实际 119.9989
    assert rep["cash"]["explained_by_corp"] is True
    assert rep["blockers"] == []
    assert any("公司行为" in w for w in rep["warnings"])
    assert rep["verdict"] == "ok"
    assert "⚠" in la.render_markdown(rep)


def test_same_gap_without_corporate_action_is_blocker():
    fill = _fill(symbol="300308.SZ", quantity=100, price=839.029305,
                 fee=10.068351660000001, filled_at="2026-09-07T09:30:00")
    state = {"account": {"initial_cash": 1000000.0, "cash": 916207.0,
                         "positions": {"300308.SZ": {"quantity": 100}}}}
    rep = la.audit_day(day="2026-09-07", symbol="300308.SZ", signals=[],
                       orders=[], fills=[fill], state=state, bars={})
    assert rep["verdict"] == "mismatch"
    assert any("现金不闭环" in b for b in rep["blockers"])


def test_render_markdown_contains_verdict_and_rows():
    rep = la.audit_day(day="2026-11-06", symbol="300319.SZ", signals=[],
                       orders=[_order()], fills=[_fill()], state=_state(), bars=_bar())
    text = la.render_markdown(rep)
    assert "账本比对 2026-11-06 300319.SZ" in text
    assert "一致" in text and "| 现金 |" in text and "| 持仓 |" in text
