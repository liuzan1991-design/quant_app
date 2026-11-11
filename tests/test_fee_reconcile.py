"""费用口径（core/fee_reconcile.py）测试。

核心用例用的是 2026-10-28 真实账户的两次快照数值：
现金 397,098.39 → 469,754.41，期间卖出 110,394 / 买入 37,670 ⇒ 费用 67.98 元。
这条同时是「commission 字段全为 0 但差额法可用」的回归锚点。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import fee_reconcile as fr

BJ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "test_outputs" / "qmt_trader_probe"
BEFORE = PROBE / "probe_20261028_075759.json"
AFTER = PROBE / "probe_20261028_085035.json"


def _epoch(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=BJ).timestamp())


def _trade(flag, vol, amount, ts=None, comm=0.0):
    return {"m_nOffsetFlag": flag, "m_nTradedVolume": vol, "m_dTradedAmount": amount,
            "m_dTradedPrice": amount / vol if vol else 0.0,
            "m_nTradedTime": ts if ts is not None else _epoch(2026, 10, 28, 13, 5),
            "m_dCommission": comm}


REAL_TRADES = [
    _trade(49, 100, 27399.0), _trade(49, 100, 27626.0),
    _trade(49, 100, 27700.0), _trade(49, 100, 27669.0),
    _trade(48, 600, 10146.0), _trade(48, 100, 1691.0),
    _trade(48, 300, 5109.0), _trade(48, 100, 1703.0),
    _trade(48, 200, 3406.0), _trade(48, 100, 1703.0),
    _trade(48, 800, 13912.0),
]


# ── commission 可用性 ───────────────────────────────────────────────────
def test_all_zero_commission_is_unusable():
    usable, why = fr.commission_usable(REAL_TRADES)
    assert usable is False and "全为 0" in why


def test_any_nonzero_commission_is_usable():
    usable, why = fr.commission_usable([_trade(48, 100, 1000.0, comm=5.0)])
    assert usable is True and "非 0" in why


def test_empty_trades_unusable():
    assert fr.commission_usable([])[0] is False


# ── 汇总与窗口 ──────────────────────────────────────────────────────────
def test_split_trades_matches_real_amounts():
    out = fr.split_trades(REAL_TRADES)
    assert out["buy_amount"] == pytest.approx(37670.0)
    assert out["sell_amount"] == pytest.approx(110394.0)
    assert out["buy_qty"] == 2200 and out["sell_qty"] == 400


def test_split_trades_ignores_unknown_flag():
    out = fr.split_trades([_trade(0, 100, 1000.0)])
    assert out["buy_amount"] == 0 and out["sell_amount"] == 0


def test_trades_in_window_is_inclusive():
    t0 = datetime(2026, 10, 28, 12, 57, tzinfo=BJ)
    t1 = datetime(2026, 10, 28, 13, 50, tzinfo=BJ)
    assert len(fr.trades_in_window(REAL_TRADES, t0, t1)) == 11
    late = [_trade(48, 100, 1000.0, ts=_epoch(2026, 10, 28, 14, 30))]
    assert fr.trades_in_window(late, t0, t1) == []


# ── 差额法 ──────────────────────────────────────────────────────────────
def test_fee_by_cash_delta_reproduces_67_98():
    out = fr.fee_by_cash_delta(cash_before=397098.39, cash_after=469754.41,
                               buy_amount=37670.0, sell_amount=110394.0)
    assert out["net_flow"] == pytest.approx(72724.0)
    assert out["fee"] == pytest.approx(67.98, abs=1e-6)
    assert out["ok"] is True


def test_negative_fee_flagged():
    out = fr.fee_by_cash_delta(cash_before=1000.0, cash_after=2000.0,
                               buy_amount=0.0, sell_amount=0.0)
    assert out["ok"] is False and "未记录的资金业务" in out["note"]


def test_decompose_estimate_math():
    d = fr.decompose(67.98, buy_amount=37670.0, sell_amount=110394.0)
    assert d["estimate"] is True
    assert d["stamp_duty"] == pytest.approx(55.197)
    assert d["transfer_fee"] == pytest.approx(1.48064)
    assert d["commission"] == pytest.approx(11.30236, abs=1e-6)
    assert d["commission_rate_pct"] == pytest.approx(0.007633, abs=1e-5)


# ── 快照解析 ────────────────────────────────────────────────────────────
def _snap(probe_at, cash, trades):
    return {"probe_at": probe_at,
            "accounts": [{"asset": {"cash": cash}, "orders": [], "trades": trades}]}


def test_cross_day_rejected():
    res = fr.resolve_from_snapshots(_snap("2026-10-27T07:00:00", 1000.0, []),
                                    _snap("2026-10-28T07:00:00", 1000.0, []))
    assert res["status"] == "insufficient_evidence" and res.get("cross_day") is True


def test_zero_trades_but_cash_moved_is_clock_suspect():
    """正是"时区搞错"的形态：窗口里没成交，现金却变了 ⇒ 先怀疑时间基准。"""
    res = fr.resolve_from_snapshots(_snap("2026-10-28T14:00:00", 1000.0, []),
                                    _snap("2026-10-28T15:00:00", 1200.0, []))
    assert res["status"] == "insufficient_evidence"
    assert res.get("clock_suspect") is True


def test_missing_cash_reported():
    res = fr.resolve_from_snapshots({"probe_at": "2026-10-28T07:00:00", "accounts": [{}]},
                                    {"probe_at": "2026-10-28T08:00:00", "accounts": [{}]})
    assert res["status"] == "insufficient_evidence" and "cash" in res["reason"]


def test_render_smoke():
    res = fr.resolve_from_snapshots(_snap("2026-10-28T06:00:00", 397098.39, []),
                                    _snap("2026-10-28T10:00:00", 469754.41, REAL_TRADES))
    text = fr.render(res)
    assert "费用口径核算" in text


# ── 真实快照回归（文件缺失则跳过，不掩盖） ───────────────────────────────
@pytest.mark.skipif(not (BEFORE.exists() and AFTER.exists()),
                    reason="缺少 2026-10-28 探针快照")
def test_real_snapshots_give_cash_delta_67_98():
    before = json.loads(BEFORE.read_text(encoding="utf-8"))
    after = json.loads(AFTER.read_text(encoding="utf-8"))
    res = fr.resolve_from_snapshots(before, after)
    assert res["status"] == "cash_delta"
    assert res["trades_in_window"] == 11
    assert res["fee"] == pytest.approx(67.98, abs=1e-6)
    assert res["decomposition"]["estimate"] is True
