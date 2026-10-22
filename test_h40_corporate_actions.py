# -*- coding: utf-8 -*-
"""H40：模拟账户公司行为处理（2026-10-21 拍板：税前入账留痕、成本冲减、历史缺口不回填）。

覆盖：纯分红 / 送转+分红 / 幂等 / 换日自动触发 / 无事件零影响 / 东财解析 / 状态持久化兼容。
全部离线（不打网络），缓存目录用 tmp_path 隔离。
"""
from __future__ import annotations

import json

import pytest

from core import corporate_actions as ca
from core.paper_broker import PaperBroker
from core.paper_store import PaperStateStore

D19_ACTION = ca.CorporateAction(ex_date="2026-10-12", dividend_per_share=1.20,
                                bonus_ratio=0.0, plan_desc="10派12.00元(含税,扣税后10.80元)")
ACTION_2024 = ca.CorporateAction(ex_date="2024-06-06", dividend_per_share=0.45,
                                 bonus_ratio=0.4, plan_desc="10转4股派4.50元(含税,扣税后4.05元)")


def _broker_with_position(cash: float = 916_087.0, qty: int = 100, cost: float = 839.1299885):
    broker = PaperBroker(cash, "2026-10-09")
    from core.paper_models import Position
    pos = Position(symbol="300308.SZ", quantity=qty, sellable_quantity=qty,
                   average_cost=cost, market_price=774.10)
    broker.account.positions["300308.SZ"] = pos
    return broker


def test_cash_dividend_d19_real():
    """纯现金分红：10-12 真实方案 10 派 12，100 股 → cash +120、成本冲减 1.20、equity +120。"""
    broker = _broker_with_position()
    equity_before = broker.account.equity()
    assert broker.apply_corporate_action(D19_ACTION, "300308.SZ") is True
    pos = broker.account.positions["300308.SZ"]
    assert broker.account.cash == pytest.approx(916_087.0 + 120.0)
    assert pos.quantity == 100
    assert pos.average_cost == pytest.approx(839.1299885 - 1.20)
    assert broker.account.equity() == pytest.approx(equity_before + 120.0)
    event = [e for e in broker.audit_log if e["event"] == "CORPORATE_ACTION"]
    assert len(event) == 1 and event[0]["cash_in"] == pytest.approx(120.0)  # 税前入账留痕


def test_bonus_plus_dividend_2024_real():
    """复合：2024-06-06 真实方案 10 转 4 派 4.5，100 股 → 140 股、cash +45、成本 (100c−45)/140。"""
    broker = _broker_with_position(cash=500_000.0, qty=100, cost=500.0)
    assert broker.apply_corporate_action(ACTION_2024, "300308.SZ") is True
    pos = broker.account.positions["300308.SZ"]
    assert pos.quantity == 140
    assert broker.account.cash == pytest.approx(500_000.0 + 45.0)  # 派息基数=送转前 100 股
    assert pos.average_cost == pytest.approx((100 * 500.0 - 45.0) / 140.0)
    # 权益守恒：市值按除权后价自然缩水，此处仅验证股数×成本 = 原总成本 − 分红
    assert pos.quantity * pos.average_cost == pytest.approx(100 * 500.0 - 45.0)


def test_idempotent_same_event():
    """幂等：同事件重复应用 False、账面不变；更旧事件也 False。"""
    broker = _broker_with_position()
    assert broker.apply_corporate_action(D19_ACTION, "300308.SZ") is True
    cash_after = broker.account.cash
    cost_after = broker.account.positions["300308.SZ"].average_cost
    assert broker.apply_corporate_action(D19_ACTION, "300308.SZ") is False
    assert broker.apply_corporate_action(
        ca.CorporateAction(ex_date="2025-10-13", dividend_per_share=0.40,
                           bonus_ratio=0.0), "300308.SZ") is False
    assert broker.account.cash == cash_after
    assert broker.account.positions["300308.SZ"].average_cost == cost_after


def test_start_new_trading_day_auto_apply(tmp_path, monkeypatch):
    """换日自动触发：缓存含 10-12 事件，start_new_trading_day("2026-10-12") 自动应用。"""
    monkeypatch.setattr(ca, "DATA_DIR", tmp_path)
    payload = {"code": "300308", "actions": [D19_ACTION.__dict__]}
    (tmp_path / "corporate_actions_300308.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    broker = _broker_with_position()
    broker.start_new_trading_day("2026-10-12")
    assert broker.account.cash == pytest.approx(916_087.0 + 120.0)
    assert broker.account.applied_corporate_actions["300308.SZ"] == "2026-10-12"
    # 送转/换日解锁：sellable_quantity 由换日循环重置为 quantity
    assert broker.account.positions["300308.SZ"].sellable_quantity == 100


def test_no_event_day_noop(tmp_path, monkeypatch):
    """无事件日：换日后 cash / 成本完全不变（guard-off 的正向对照）。"""
    monkeypatch.setattr(ca, "DATA_DIR", tmp_path)
    broker = _broker_with_position()
    broker.start_new_trading_day("2026-10-13")
    assert broker.account.cash == 916_087.0
    assert broker.account.positions["300308.SZ"].average_cost == pytest.approx(839.1299885)


def test_no_position_still_records():
    """无持仓也记账：防重启后重复扫描同一事件。"""
    broker = PaperBroker(100_000.0, "2026-10-11")
    assert broker.apply_corporate_action(D19_ACTION, "300308.SZ") is False
    assert broker.account.applied_corporate_actions["300308.SZ"] == "2026-10-12"


def test_build_url_is_ascii_encodable():
    """H48：请求行只能 encode('ascii') ⇒ URL 里的中文必须先百分号编码。

    生产首日（2026-10-22 04:32:08）真实抛过
    ``UnicodeEncodeError: 'ascii' codec can't encode characters in position 112-115``
    —— 正是 filter 里「实施分配」4 个汉字。原 8 例全绿却漏掉它，因为
    ``fetch_actions`` 在测试里被整体替换（不打网络）⇒ **URL 构造零覆盖**。
    """
    url = ca.build_url("300308")
    url.encode("ascii")  # 不抛 = 通过（这是本用例的核心断言）
    assert "RPT_SHAREBONUS_DET" in url
    assert "300308" in url
    assert "%E5%AE%9E%E6%96%BD%E5%88%86%E9%85%8D" in url  # 实施分配
    assert "ASSIGN_PROGRESS" in url


def test_parse_eastmoney_payload():
    """东财 JSON 解析：null 送转字段 → 0、每10股 → 每股、按日期排序去重。"""
    payload = {"result": {"data": [
        {"EX_DIVIDEND_DATE": "2026-10-12 00:00:00", "PRETAX_BONUS_RMB": 12,
         "BONUS_IT_RATIO": None, "IMPL_PLAN_PROFILE": "10派12.00元(含税)"},
        {"EX_DIVIDEND_DATE": "2024-06-06 00:00:00", "PRETAX_BONUS_RMB": 4.5,
         "BONUS_IT_RATIO": 4, "IMPL_PLAN_PROFILE": "10转4股派4.50元(含税)"},
        {"EX_DIVIDEND_DATE": None, "PRETAX_BONUS_RMB": 1},
    ]}}
    actions = ca._parse_payload(payload)
    assert [a.ex_date for a in actions] == ["2024-06-06", "2026-10-12"]
    assert actions[1].dividend_per_share == pytest.approx(1.2)
    assert actions[1].bonus_ratio == 0.0
    assert actions[0].bonus_ratio == pytest.approx(0.4)


def test_store_roundtrip_and_legacy_snapshot(tmp_path):
    """状态持久化：applied_corporate_actions 随 save/load 保留；旧快照缺字段可加载。"""
    broker = _broker_with_position()
    broker.apply_corporate_action(D19_ACTION, "300308.SZ")
    store = PaperStateStore(tmp_path)
    store.save(broker)
    loaded = store.load()
    assert loaded.account.applied_corporate_actions["300308.SZ"] == "2026-10-12"
    assert loaded.account.cash == pytest.approx(916_087.0 + 120.0)
    # 旧快照兼容：删掉新字段后 load 不报错、字段取空
    payload = json.loads((tmp_path / "paper_state.json").read_text(encoding="utf-8"))
    payload["account"].pop("applied_corporate_actions")
    (tmp_path / "paper_state.json").write_text(json.dumps(payload, ensure_ascii=False),
                                               encoding="utf-8")
    legacy = store.load()
    assert legacy.account.applied_corporate_actions == {}
