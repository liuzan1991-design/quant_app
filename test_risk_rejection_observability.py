# -*- coding: utf-8 -*-
"""H9 观测增强回归：风控拒绝必须留下结构化审计事件。

背景（2026-09-18，H9/P0-5 引出的观测缺口）：
    风控拒绝此前是静默的——orders 里只有 reject_reason 字符串，
    paper_audit.jsonl 里没有拒绝事件。导致「策略无信号」与「信号被
    风控拒」在数据上无法区分，D8 网格零成交需读四层代码才能归因。

本文件锁定两条不变量：
    1. 被拒订单必须产生 event=RISK_REJECTED 的审计事件，
       且 violations 为结构化 code 列表（如 symbol_position_limit）；
    2. 观测增强不改变交易行为——订单状态仍为 REJECTED、拒因文本不变、
       通过的订单照常成交、审计事件能被 PaperStateStore 持久化。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import MarketSnapshot, OrderRequest, OrderSide
from core.paper_store import PaperStateStore
from core.risk_manager import RiskConfig, RiskManager

NOW = datetime(2026, 9, 18, 10, 0, 0)
SYMBOL = "300308.SZ"


def _req(oid: str, side: OrderSide, qty: int) -> OrderRequest:
    return OrderRequest(client_order_id=oid, symbol=SYMBOL, side=side,
                        quantity=qty, created_at=NOW, strategy_id="h9-observe")


def _snap(price: float = 926.43, volume: int = 10_000_000,
          ts: datetime = NOW) -> MarketSnapshot:
    return MarketSnapshot(symbol=SYMBOL, timestamp=ts, last_price=price, volume=volume)


def _live_risk() -> RiskManager:
    """复刻观察期 live 风控参数（live_match_loop.py:54-55）。"""
    return RiskManager(RiskConfig(max_symbol_position_pct=0.10,
                                  max_account_position_pct=0.90,
                                  max_order_value_pct=0.10))


def test_rejected_order_writes_risk_rejected_audit_event():
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=_live_risk(),
                         config=PaperBrokerConfig(slippage_bps=0,
                                                  max_volume_participation=1.0))
    # D8 真实持仓快照：底仓 100 股已占 9.18%，再买 100 股触发 10% 硬门。
    from core.paper_models import Position
    broker.account.positions[SYMBOL] = Position(
        symbol=SYMBOL, quantity=100, sellable_quantity=100,
        average_cost=896.0, market_price=926.43, today_bought_quantity=0)
    broker.account.cash = 916_087.0

    order = broker.submit_order(_req("h9-obs-rej", OrderSide.BUY, 100), _snap(), NOW)
    assert order.status.value == "REJECTED"

    events = [e for e in broker.audit_log if e.get("event") == "RISK_REJECTED"]
    assert len(events) == 1, f"应恰好 1 条 RISK_REJECTED，实际 {len(events)}"
    e = events[0]
    assert e["client_order_id"] == "h9-obs-rej"
    assert e["side"] == "BUY"
    assert e["symbol"] == SYMBOL
    assert e["quantity"] == 100
    assert e["strategy_id"] == "h9-observe"
    assert "symbol_position_limit" in e["violations"], \
        f"violations 应含 symbol_position_limit，实际 {e['violations']}"


def test_observability_change_does_not_alter_trading_behavior():
    """拒单状态与拒因文本必须与增强前完全一致；通过单照常成交。"""
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=_live_risk(),
                         config=PaperBrokerConfig(slippage_bps=0,
                                                  max_volume_participation=1.0))
    # 空仓买 100 股 @926.43 = 9.26%，应通过并成交。
    order = broker.submit_order(_req("h9-obs-ok", OrderSide.BUY, 100), _snap(), NOW)
    assert order.status.value == "FILLED"
    assert order.filled_quantity == 100
    # 被拒单：价格 1010 使 100 股 > 10% 单笔门。
    order2 = broker.submit_order(_req("h9-obs-rej2", OrderSide.BUY, 100),
                                 _snap(price=1010.0), NOW)
    assert order2.status.value == "REJECTED"
    assert "单股仓位超过限制" in order2.reject_reason, \
        f"拒因文本不应改变，实际：{order2.reject_reason}"
    # 通过单不得产生 RISK_REJECTED 事件。
    rejects = [e for e in broker.audit_log if e.get("event") == "RISK_REJECTED"]
    assert len(rejects) == 1 and rejects[0]["client_order_id"] == "h9-obs-rej2"


def test_risk_rejected_event_persists_to_paper_audit_jsonl():
    """审计事件必须能被 PaperStateStore 落盘到 paper_audit.jsonl。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        store = PaperStateStore(Path(tmp))
        broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=_live_risk(),
                             config=PaperBrokerConfig(slippage_bps=0,
                                                      max_volume_participation=1.0))
        broker.submit_order(_req("h9-obs-persist", OrderSide.BUY, 100),
                            _snap(price=1010.0), NOW)
        store.save(broker)
        lines = [json.loads(line) for line in
                 store.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        events = [e for e in lines if e.get("event") == "RISK_REJECTED"]
        assert len(events) == 1, "paper_audit.jsonl 中应恰好 1 条 RISK_REJECTED"
        assert events[0]["violations"], "落盘事件必须保留结构化 violations"
        # 幂等：重复 save 不得重复写审计事件。
        store.save(broker)
        lines2 = [json.loads(line) for line in
                  store.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        events2 = [e for e in lines2 if e.get("event") == "RISK_REJECTED"]
        assert len(events2) == 1, f"重复 save 后不得重复写，实际 {len(events2)} 条"


def main() -> None:
    tests = [
        test_rejected_order_writes_risk_rejected_audit_event,
        test_observability_change_does_not_alter_trading_behavior,
        test_risk_rejected_event_persists_to_paper_audit_jsonl,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\nH9 观测增强：{len(tests)} 项全部通过")


if __name__ == "__main__":
    main()