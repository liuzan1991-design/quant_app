"""委托归属（core/order_intent.py）测试。

order fixture 按 2026-10-28 真实柜台形态构造：`order_id`/`m_nOrderID` 为 0，
`m_strOrderSysID` 是唯一键，手工单 remark 为空串。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core import order_intent as oi

BJ = timezone(timedelta(hours=8))


def _epoch(y, mo, d, h, mi, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=BJ).timestamp())


def _qmt(sysid="1525", flag=48, vol=100, price=271.5, ts=None, remark="",
         code="300319.SZ"):
    return {"m_strOrderSysID": sysid, "m_nOffsetFlag": flag, "m_nOrderVolume": vol,
            "m_dPrice": price, "m_nOrderTime": ts if ts is not None else _epoch(2026, 11, 6, 9, 31, 5),
            "m_strStockCode": code, "m_strOrderRemark": remark,
            "m_strStrategyName": "", "m_nOrderID": 0, "order_id": 0}


def _order(**kw):
    return oi.normalize_qmt_order(_qmt(**kw))


# ── 意图构造 ────────────────────────────────────────────────────────────
def test_remark_is_ascii_and_short():
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=14.0,
                       strategy="grid_trade", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    assert it.remark == "QAab12cd34"
    assert len(it.remark) <= oi.REMARK_MAX_LEN
    assert it.remark.isascii()


def test_symbol_normalized():
    it = oi.new_intent(symbol="300319", side="BUY", quantity=100, price=14.0,
                       strategy="s", created_at="2026-11-06T09:31:00")
    assert it.symbol == "300319.SZ"


@pytest.mark.parametrize("kw", [
    {"side": "HOLD"}, {"quantity": 0}, {"price": -1.0}, {"price": 0},
])
def test_invalid_intent_rejected(kw):
    base = dict(symbol="300319.SZ", side="BUY", quantity=100, price=14.0,
                strategy="s", created_at="2026-11-06T09:31:00")
    base.update(kw)
    with pytest.raises(oi.IntentError):
        oi.new_intent(**base)


# ── 柜台委托规整 ────────────────────────────────────────────────────────
def test_offset_flag_maps_direction_not_m_direction():
    assert _order(flag=48)["side"] == "BUY"
    assert _order(flag=49)["side"] == "SELL"


def test_unknown_offset_flag_rejected():
    with pytest.raises(oi.IntentError):
        oi.normalize_qmt_order(_qmt(flag=0))


def test_epoch_converted_to_beijing():
    o = _order(ts=_epoch(2026, 11, 6, 9, 31, 5))
    assert o["time"].utcoffset() == timedelta(hours=8)
    assert o["time"].strftime("%Y-%m-%d %H:%M:%S") == "2026-11-06 09:31:05"


def test_order_id_zero_is_kept_but_not_used_as_key():
    o = _order()
    assert o["order_id"] == 0 and o["sysid"] == "1525"


# ── 绑定 ────────────────────────────────────────────────────────────────
def test_bind_by_remark():
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="grid_trade", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order(remark=it.remark)])
    assert res.ok and res.sysid_of(it.intent_id) == "1525"
    assert res.matched[it.intent_id]["by"] == "remark"


def test_bind_by_attrs_when_remark_empty():
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="grid_trade", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order(remark="", price=271.52)])   # 差 0.007% < 0.02%
    assert res.ok
    assert res.matched[it.intent_id]["by"] == "attrs"


def test_price_out_of_tolerance_not_bound():
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order(price=280.0)])
    assert res.unbound == [it.intent_id]
    assert res.orphan_orders


def test_real_price_gap_not_merged():
    """真实 301717 四笔卖单：相邻价差 0.101%，默认容差下必须逐笔唯一。"""
    prices = [273.91, 276.26, 276.90, 276.62]
    secs = [0, 278, 319, 378]      # 相对 12:59:42
    base = _epoch(2026, 10, 28, 12, 59, 42)
    orders = [oi.normalize_qmt_order(_qmt(sysid=str(1500 + i), flag=49, vol=100,
                                          price=p, ts=base + s,
                                          code="301717.SZ"))
              for i, (p, s) in enumerate(zip(prices, secs))]
    intents = [oi.new_intent(symbol="301717.SZ", side="SELL", quantity=100, price=p,
                             strategy="grid_trade",
                             created_at=datetime.fromtimestamp(base + s, BJ).strftime(
                                 "%Y-%m-%dT%H:%M:%S"),
                             intent_id=f"aa00000{i}")
               for i, (p, s) in enumerate(zip(prices, secs))]
    res = oi.bind(intents, orders)
    assert res.ok and len(res.matched) == 4 and res.orphan_orders == []


def test_loose_tolerance_would_merge_neighbours():
    """反例：旧默认 0.2%/180s 会把 276.26 与 276.62（差 0.13%、隔 100s）判成歧义。"""
    base = _epoch(2026, 10, 28, 13, 4, 20)
    orders = [oi.normalize_qmt_order(_qmt(sysid="1557", flag=49, vol=100, price=276.26,
                                          ts=base, code="301717.SZ")),
              oi.normalize_qmt_order(_qmt(sysid="1569", flag=49, vol=100, price=276.62,
                                          ts=base + 100, code="301717.SZ"))]
    it = oi.new_intent(symbol="301717.SZ", side="SELL", quantity=100, price=276.26,
                       strategy="s", created_at=datetime.fromtimestamp(base, BJ).strftime(
                           "%Y-%m-%dT%H:%M:%S"), intent_id="aa000000")
    assert oi.bind([it], orders).ok is True
    assert oi.bind([it], orders, price_tol_pct=0.2, time_window_sec=180).ok is False


def test_time_outside_window_not_bound():
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order(ts=_epoch(2026, 11, 6, 9, 45))])
    assert res.unbound == [it.intent_id]


def test_two_candidates_make_it_ambiguous():
    """同一属性有两笔委托 ⇒ 必须停手，不得按顺序抢一个。"""
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order(sysid="1525"), _order(sysid="1557")])
    assert not res.ok
    assert res.ambiguous and res.ambiguous[0]["by"] == "attrs"


def test_one_order_claimed_by_two_intents_is_ambiguous():
    a = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                      strategy="s", created_at="2026-11-06T09:31:00", intent_id="aaaa1111")
    b = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                      strategy="s", created_at="2026-11-06T09:31:00", intent_id="bbbb2222")
    res = oi.bind([a, b], [_order()])
    assert not res.ok
    assert {x["by"] for x in res.ambiguous} == {"shared_order"}


def test_orphan_order_when_no_intent():
    res = oi.bind([], [_order()])
    assert len(res.orphan_orders) == 1 and res.ok


def test_frozen_binding_reused():
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order()], frozen={it.intent_id: "1525"})
    assert res.matched[it.intent_id]["by"] == "frozen"


def test_stale_frozen_binding_falls_back():
    """固化表指向的 sysid 已不存在 ⇒ 重新按属性匹配，不沿用脏绑定。"""
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order()], frozen={it.intent_id: "9999"})
    assert res.matched[it.intent_id]["by"] == "attrs"


def test_frozen_symbol_conflict_rejected():
    """固化表把这条意图指到另一个标的的委托上 ⇒ 不认，重新匹配后落 unbound。"""
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    other = oi.normalize_qmt_order(_qmt(code="301717.SZ", remark=""))
    res = oi.bind([it], [other], frozen={it.intent_id: "1525"})
    assert it.intent_id in res.unbound


# ── 固化表落盘 ──────────────────────────────────────────────────────────
def test_frozen_roundtrip(tmp_path):
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order()])
    path = tmp_path / "frozen.jsonl"
    assert oi.append_frozen(path, oi.to_frozen_records(res)) == 1
    assert oi.load_frozen(path) == {it.intent_id: "1525"}


def test_load_frozen_skips_bad_lines(tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text('{"intent_id":"aa","sysid":"1"}\n不是 json\n\n', encoding="utf-8")
    assert oi.load_frozen(p) == {"aa": "1"}


def test_summary_text():
    it = oi.new_intent(symbol="300319.SZ", side="BUY", quantity=100, price=271.5,
                       strategy="s", created_at="2026-11-06T09:31:00",
                       intent_id="ab12cd34")
    res = oi.bind([it], [_order()])
    assert res.summary() == "owned=1 ambiguous=0 orphan=0 unbound=0"
