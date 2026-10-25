# -*- coding: utf-8 -*-
"""QMT 交易适配器的测试 —— 重点验两件事：
① 参数映射对不对（代码后缀 / 买卖常量 / 数量单位 / 价格类型）；
② **没 arm 时绝不下单**（国盛 QMT 无模拟盘，误触即真钱）。
"""
from __future__ import annotations

import pytest

from core import qmt_broker as qb


class FakeTrader:
    def __init__(self, order_id: int = 12345, cancel_rc: int = 0):
        self.calls: list[tuple] = []
        self.order_id = order_id
        self.cancel_rc = cancel_rc

    def order_stock(self, *args):
        self.calls.append(("order_stock",) + args)
        return self.order_id

    def cancel_order_stock(self, *args):
        self.calls.append(("cancel_order_stock",) + args)
        return self.cancel_rc

    def query_stock_asset(self, acc):
        self.calls.append(("query_stock_asset", acc))
        return "ASSET"

    def query_stock_positions(self, acc):
        self.calls.append(("query_stock_positions", acc))
        return ["POS"]

    def query_stock_orders(self, acc):
        return []

    def query_stock_trades(self, acc):
        return []


@pytest.fixture
def fake(monkeypatch):
    """隔离 xtconstant 依赖：常量用可识别的字符串代替，便于断言实参顺序。"""
    monkeypatch.setattr(qb, "_const", lambda name: f"<{name}>")
    t = FakeTrader()
    return t, qb.QmtBroker(t, "ACC")


@pytest.fixture
def arm(monkeypatch):
    monkeypatch.setenv(qb.ARM_ENV, "1")


# ── 纯映射 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("300308", "300308.SZ"),
    ("000001", "000001.SZ"),
    ("600900", "600900.SH"),
    ("688981", "688981.SH"),
    ("900901", "900901.SH"),
    ("300308.SZ", "300308.SZ"),
    (" 600900.sh ", "600900.SH"),
])
def test_to_qmt_code(raw, want) -> None:
    assert qb.to_qmt_code(raw) == want


@pytest.mark.parametrize("bad", ["3003088", "abc", "", "12345"])
def test_to_qmt_code_rejects(bad) -> None:
    with pytest.raises(qb.QmtBrokerError):
        qb.to_qmt_code(bad)


@pytest.mark.parametrize("side,want", [
    ("BUY", "STOCK_BUY"), ("buy", "STOCK_BUY"), ("买入", "STOCK_BUY"),
    ("SELL", "STOCK_SELL"), ("卖出", "STOCK_SELL"),
])
def test_side_to_const_name(side, want) -> None:
    assert qb.side_to_const_name(side) == want


def test_side_rejects_unknown() -> None:
    with pytest.raises(qb.QmtBrokerError):
        qb.side_to_const_name("HOLD")


# ── arm 闸门 ────────────────────────────────────────────────────────────────
def test_disarmed_by_default(monkeypatch) -> None:
    monkeypatch.delenv(qb.ARM_ENV, raising=False)
    assert qb.armed() is False
    with pytest.raises(qb.QmtTradingDisarmed):
        qb.require_armed()


def test_submit_disarmed_never_touches_trader(monkeypatch) -> None:
    """未 arm ⇒ 连 trader 都不该碰（不能"先下单再检查"）。"""
    monkeypatch.delenv(qb.ARM_ENV, raising=False)
    t = FakeTrader()
    b = qb.QmtBroker(t, "ACC")
    with pytest.raises(qb.QmtTradingDisarmed):
        b.submit("300308", "BUY", 100, 772.8)
    assert t.calls == [], f"未 arm 竟然调用了券商接口：{t.calls}"


def test_cancel_disarmed_raises(monkeypatch) -> None:
    monkeypatch.delenv(qb.ARM_ENV, raising=False)
    t = FakeTrader()
    with pytest.raises(qb.QmtTradingDisarmed):
        qb.QmtBroker(t, "ACC").cancel(1)
    assert t.calls == []


# ── 下单实参映射 ────────────────────────────────────────────────────────────
def test_submit_maps_args_in_official_order(arm, fake) -> None:
    """官方签名顺序：account, code, order_type, volume, price_type, price,
    strategy_name, remark —— 顺序错一位就是下错单。"""
    t, b = fake
    r = b.submit("300308", "BUY", 200, 772.8, remark="grid_L1")
    assert r.ok and r.order_id == 12345
    name, *args = t.calls[0]
    assert name == "order_stock"
    assert args == ["ACC", "300308.SZ", "<STOCK_BUY>", 200,
                    "<FIX_PRICE>", 772.8, "quant_app", "grid_L1"], args


def test_submit_sell_side(arm, fake) -> None:
    t, b = fake
    b.submit("600900", "SELL", 100, 28.5)
    assert t.calls[0][2] == "600900.SH"
    assert t.calls[0][3] == "<STOCK_SELL>"
    assert t.calls[0][4] == 100, "数量单位应为股，不得再乘 100"


def test_submit_order_id_minus_one_is_failure(arm, monkeypatch) -> None:
    monkeypatch.setattr(qb, "_const", lambda name: f"<{name}>")
    t = FakeTrader(order_id=-1)
    r = qb.QmtBroker(t, "ACC").submit("300308", "BUY", 100, 10.0)
    assert r.ok is False and r.order_id is None


@pytest.mark.parametrize("qty", [0, -100, 150, 99])
def test_submit_rejects_bad_quantity(arm, fake, qty) -> None:
    t, b = fake
    with pytest.raises(qb.QmtBrokerError):
        b.submit("300308", "BUY", qty, 10.0)
    assert t.calls == []


@pytest.mark.parametrize("price", [0, -1.0, None])
def test_submit_rejects_bad_price(arm, fake, price) -> None:
    t, b = fake
    with pytest.raises(qb.QmtBrokerError):
        b.submit("300308", "BUY", 100, price)
    assert t.calls == []


# ── 只读方法：不 arm 也能用 ─────────────────────────────────────────────────
def test_readonly_works_unarmed(monkeypatch, fake) -> None:
    monkeypatch.delenv(qb.ARM_ENV, raising=False)
    t, b = fake
    assert b.query_asset() == "ASSET"
    assert b.query_positions() == ["POS"]
    assert [c[0] for c in t.calls] == ["query_stock_asset", "query_stock_positions"]


def test_cancel_armed_rc_zero_is_true(arm, fake) -> None:
    t, b = fake
    assert b.cancel(12345) is True
    assert t.calls[0] == ("cancel_order_stock", "ACC", 12345)
