# -*- coding: utf-8 -*-
"""H15 费用口径修正 —— paper_broker 默认费率与实际一致（2026-09-27 用户确认 + 监管文件）。

依据：`A16实盘对账_设计方案.md` §2.5 待改清单三项：
  1. 印花税   0.001 → 0.0005（2023-08-28 起卖出单边减半）
  2. 过户费   0.00002 → 0.00001（2022-04-29 起下调 50%）
  3. 佣金     0.0001/最低 0 → 0.00008/最低 1.0（万 0.8 / 最低 1 元，用户确认）
A16 §2.2 解读 A 的复算数值（100 股 × 895.86 元）作为绝对值锚点：
  买 8.06274 / 卖 52.85574。
⚠️ 「免规费」解读 A/B 未裁定 ⇒ 本测试按解读 A（当前假定，方案 §2.4）；
   若首单对账裁定为 B，须另立项加规费科目（结构性改动），届时本测试同步修订。
"""
import math

import pytest

from core.paper_broker import OrderSide, PaperBroker, PaperBrokerConfig


def test_h15_default_rates_confirmed():
    cfg = PaperBrokerConfig()
    assert cfg.commission_rate == pytest.approx(0.00008)
    assert cfg.min_commission == pytest.approx(1.0)
    assert cfg.stamp_tax_rate == pytest.approx(0.0005)
    assert cfg.transfer_fee_rate == pytest.approx(0.00001)
    # 滑点与参与率不在本批改动面，保持原值
    assert cfg.slippage_bps == pytest.approx(5.0)
    assert cfg.max_volume_participation == pytest.approx(0.10)


def test_h15_fee_buy_matches_a16_anchor():
    """买入 1 手 @895.86：佣金 7.16688 + 过户 0.89586 = 8.06274（A16 §2.2 解读 A）。"""
    broker = PaperBroker.__new__(PaperBroker)          # 只测 _calculate_fee，不建账户
    broker.config = PaperBrokerConfig()
    fee = broker._calculate_fee(OrderSide.BUY, 895.86, 100)
    assert fee == pytest.approx(8.06274, abs=1e-9)


def test_h15_fee_sell_matches_a16_anchor():
    """卖出 1 手 @895.86：佣金 7.16688 + 印花 44.793 + 过户 0.89586 = 52.85574。"""
    broker = PaperBroker.__new__(PaperBroker)
    broker.config = PaperBrokerConfig()
    fee = broker._calculate_fee(OrderSide.SELL, 895.86, 100)
    assert fee == pytest.approx(52.85574, abs=1e-9)


def test_h15_min_commission_floor():
    """小额触发最低 1 元：100 股 × 100 元 = 10,000 → 万 0.8 = 0.8 < 1 ⇒ 取 1.0（仅佣金项）。"""
    broker = PaperBroker.__new__(PaperBroker)
    broker.config = PaperBrokerConfig()
    fee = broker._calculate_fee(OrderSide.BUY, 100.0, 100)
    assert fee == pytest.approx(1.0 + 10000.0 * 0.00001, abs=1e-9)


def test_h15_stamp_sell_only():
    broker = PaperBroker.__new__(PaperBroker)
    broker.config = PaperBrokerConfig()
    value = 895.86 * 100
    buy = broker._calculate_fee(OrderSide.BUY, 895.86, 100)
    sell = broker._calculate_fee(OrderSide.SELL, 895.86, 100)
    assert math.isclose(sell - buy, value * 0.0005, abs_tol=1e-9)


def test_h15_strategy_param_meta_confirmed():
    """策略层 param_meta 默认费率同步修正（H15 影响面扩展：grid/ma_swing/intraday_t）。

    live 链 params 由 default_params() → freeze_params() 生成，若 param_meta 仍旧费率，
    ma_swing 快速路径卖出费用预估（ma_swing_live.py:288-293）将继续用旧口径。
    """
    from strategies import get_strategy
    for sid in ("grid_trade", "ma_swing", "intraday_t"):
        p = get_strategy(sid).default_params()
        assert p["commission_rate"] == pytest.approx(0.00008), sid
        assert p["min_commission"] == pytest.approx(1.0), sid
        assert p["stamp_tax_rate"] == pytest.approx(0.0005), sid
        assert p["transfer_fee_rate"] == pytest.approx(0.00001), sid
