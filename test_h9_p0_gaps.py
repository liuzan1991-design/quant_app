# -*- coding: utf-8 -*-
"""H9 五项 P0 安全缺口的回归测试（测试先行，只复现、不改生产行为）。

对应《量化研究规范_纠偏记录.md》H9 条目。
本文件的性质说明：
- 这些测试**断言的是"缺陷存在"**，即断言当前（有缺陷的）行为。
- 因此它们**不是**"修复后应保持通过"的测试，而是**缺陷看门狗**：
  一旦 P0 被修复，对应用例会**失败**——这正是期望行为，提示"H9 已修复，请更新测试语义"。
- 观察期内**只运行、只记录**，不因此修改生产代码。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_models import MarketSnapshot, OrderRequest, OrderSide, Position
from core.risk_manager import RiskConfig, RiskManager

NOW = datetime(2026, 9, 18, 10, 0, 0)
SYMBOL = "300308.SZ"


def _req(oid: str, side: OrderSide, qty: int) -> OrderRequest:
    return OrderRequest(client_order_id=oid, symbol=SYMBOL, side=side,
                        quantity=qty, created_at=NOW, strategy_id="h9")


def _snap(price: float = 100.0, volume: int = 10_000_000,
          ts: datetime = NOW) -> MarketSnapshot:
    return MarketSnapshot(symbol=SYMBOL, timestamp=ts, last_price=price, volume=volume)


def _loose_risk() -> RiskManager:
    """放宽所有仓位/金额门，用于隔离观察单一规则。"""
    return RiskManager(RiskConfig(max_symbol_position_pct=1.0,
                                  max_account_position_pct=1.0,
                                  max_order_value_pct=1.0,
                                  max_daily_loss_pct=1.0,
                                  max_symbol_cumulative_loss_pct=1.0))


def test_p0_1_live_contract_makes_quote_delay_always_zero():
    """P0-1：live 契约下 now == market.timestamp ⇒ 延迟恒为 0，120 秒阈值永不触发。"""
    ts = NOW
    broker = PaperBroker(1_000_000, "2026-09-18")
    snapshot = _snap(ts=ts)
    # 复刻 paper_replay.py:102 的调用契约：ts 同时充当 now 与 snapshot.timestamp
    delay = max(0.0, (ts - snapshot.timestamp).total_seconds())
    assert delay == 0.0, "live 契约下延迟应为 0（此断言即缺陷本身）"

    order = broker.submit_order(_req("h9-p1-fresh", OrderSide.BUY, 100), snapshot, ts)
    assert order.status.value == "FILLED", "延迟 0 时不应被 stale_quote 拦下"


def test_p0_1_rule_itself_is_alive():
    """P0-1 对照：规则本身有效——真陈旧行情确实会被拦（证明是"契约"问题而非"规则"问题）。"""
    broker = PaperBroker(1_000_000, "2026-09-18")
    stale = _snap(ts=NOW - timedelta(seconds=600))
    order = broker.submit_order(_req("h9-p1-stale", OrderSide.BUY, 100), stale, NOW)
    assert order.status.value == "REJECTED"
    assert "行情延迟" in order.reject_reason


def test_p0_2_risk_and_match_disagree_on_sellable_quantity():
    """P0-2：昨仓+今买时，风控与撮合对"可卖量"口径不一致（1000 vs 900）⇒ 合法委托被静默缩量。"""
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=_loose_risk(),
                         config=PaperBrokerConfig(slippage_bps=0, max_volume_participation=1.0))
    broker.account.positions[SYMBOL] = Position(
        symbol=SYMBOL, quantity=1000, sellable_quantity=1000,
        average_cost=100.0, market_price=100.0, today_bought_quantity=0)
    broker.account.cash = 900_000.0

    broker.submit_order(_req("h9-p2-buy", OrderSide.BUY, 100), _snap(), NOW)
    pos = broker.account.positions[SYMBOL]
    assert (pos.quantity, pos.sellable_quantity, pos.today_bought_quantity) == (1100, 1000, 100)

    risk_view = pos.sellable_quantity                                   # risk_manager.py:93
    match_view = max(0, pos.sellable_quantity - pos.today_bought_quantity)  # paper_broker.py:127
    assert risk_view == 1000 and match_view == 900, "两处口径应不一致（缺陷本身）"

    sell = broker.submit_order(_req("h9-p2-sell", OrderSide.SELL, 1000), _snap(), NOW)
    assert sell.status.value == "PARTIALLY_FILLED"
    assert sell.filled_quantity == 900, "应卖 1000 实际只成 900（被二次扣减）"


def test_p0_3_partial_fill_continuation_bypasses_risk_recheck():
    """P0-3：部分成交后跨 bar 续成不重新过风控 ⇒ 单日亏损停机线可被绕过。"""
    broker = PaperBroker(1_000_000, "2026-09-18",
                         risk_manager=RiskManager(RiskConfig(max_symbol_position_pct=1.0,
                                                            max_account_position_pct=1.0,
                                                            max_order_value_pct=1.0)),
                         config=PaperBrokerConfig(slippage_bps=0, max_volume_participation=0.10))
    first = broker.submit_order(_req("h9-p3", OrderSide.BUY, 1000), _snap(volume=1000), NOW)
    assert first.status.value == "PARTIALLY_FILLED"
    filled_before = first.filled_quantity

    # 账户已远超单日亏损停机线
    broker.account.daily_realized_pnl = -500_000.0

    # 注意：first 与返回的 order 是同一对象，必须先取 filled_quantity 快照再比较
    continued = broker.match_order("h9-p3", _snap(volume=10_000_000), NOW + timedelta(minutes=1))
    assert continued.filled_quantity > filled_before, \
        "停机线已触发仍继续成交 ⇒ 续成绕过风控（缺陷本身）"


def test_p0_4_stop_loss_streak_has_no_production_caller():
    """P0-4：生产代码无 record_stop_loss 调用点 ⇒ 连续止损熔断永不触发。

    扫描范围：项目内 .py，排除 ① 测试文件 ② 缓存/备份目录 ③ 以 `_` 开头的临时探针脚本
    （本次核查新建的 `_h9_verify_p0.py` 就属此类——它自身会引用这两个函数名，
    若不排除会让本用例自我误报，首版即踩此坑）。
    """
    callers = []
    for path in APP_DIR.rglob("*.py"):
        name = path.name
        if name.startswith("test_") or name.startswith("_"):
            continue
        if "__pycache__" in str(path) or "_h8_backup" in str(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            continue
        for match in re.finditer(r"\brecord_stop_loss\b|\brecord_profitable_exit\b", text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line = text[line_start:text.find("\n", match.start())]
            if re.match(r"\s*def\s", line):
                continue  # 排除定义行
            callers.append(f"{path.relative_to(APP_DIR)}:{text[:match.start()].count(chr(10)) + 1}")
    assert callers == [], f"生产代码应无调用点（缺陷本身），实际发现：{callers}"


def test_p0_4_gate_works_when_manually_invoked():
    """P0-4 对照：门本身可用——手工累计 3 次止损后确实被拦。"""
    broker = PaperBroker(1_000_000, "2026-09-18",
                         risk_manager=RiskManager(RiskConfig(stop_loss_streak_limit=3,
                                                            pause_trading_days=2,
                                                            max_symbol_position_pct=1.0,
                                                            max_account_position_pct=1.0,
                                                            max_order_value_pct=1.0)))
    for _ in range(3):
        broker.risk_manager.record_stop_loss(broker.account, SYMBOL, NOW.date())
    blocked = broker.submit_order(_req("h9-p4", OrderSide.BUY, 100), _snap(), NOW)
    assert blocked.status.value == "REJECTED"
    assert "暂停至" in blocked.reject_reason


def test_p0_5_position_gate_blocks_grid_add_on():
    """P0-5：建底仓已占 9.18%，10% 单股硬门使网格再买 100 股即被拒（敞口被顶死）。

    注意本用例固化的是「观察期 live 风控参数」下的行为：
    max_symbol_position_pct=0.10 / max_order_value_pct=0.10（live_match_loop.py:54）。
    """
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.10,
                                  max_account_position_pct=0.90,
                                  max_order_value_pct=0.10))
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=risk,
                         config=PaperBrokerConfig(slippage_bps=5, max_volume_participation=0.10))
    # D8 真实持仓快照
    broker.account.positions[SYMBOL] = Position(
        symbol=SYMBOL, quantity=100, sellable_quantity=100,
        average_cost=896.0, market_price=926.43, today_bought_quantity=0)
    broker.account.cash = 916_087.0

    decision = risk.evaluate(_req("h9-p5", OrderSide.BUY, 100), broker.account,
                             _snap(price=926.43), NOW)
    assert not decision.passed, "10% 硬门下再买 100 股应被拒（缺陷本身）"
    codes = {v.code for v in decision.violations}
    assert "symbol_position_limit" in codes


def test_p0_5_ma_swing_first_signal_blocked_above_threshold():
    """P0-5：ma_swing 空仓时，价格 >1008 的首个信号也会被 10% 单笔门拒绝。"""
    risk = RiskManager(RiskConfig(max_symbol_position_pct=0.10,
                                  max_account_position_pct=0.90,
                                  max_order_value_pct=0.10))
    broker = PaperBroker(1_000_000, "2026-09-18", risk_manager=risk)

    ok = risk.evaluate(_req("h9-p5-ma-ok", OrderSide.BUY, 100), broker.account,
                       _snap(price=1000.0), NOW)
    assert ok.passed, "1000 元时 100 股恰好 10.00%，应通过（边界）"

    blocked = risk.evaluate(_req("h9-p5-ma-blocked", OrderSide.BUY, 100), broker.account,
                            _snap(price=1010.0), NOW)
    assert not blocked.passed, "1010 元时 100 股超 10%，应被拒（缺陷本身）"


if __name__ == "__main__":
    tests = [
        test_p0_1_live_contract_makes_quote_delay_always_zero,
        test_p0_1_rule_itself_is_alive,
        test_p0_2_risk_and_match_disagree_on_sellable_quantity,
        test_p0_3_partial_fill_continuation_bypasses_risk_recheck,
        test_p0_4_stop_loss_streak_has_no_production_caller,
        test_p0_4_gate_works_when_manually_invoked,
        test_p0_5_position_gate_blocks_grid_add_on,
        test_p0_5_ma_swing_first_signal_blocked_above_threshold,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\nH9 缺陷看门狗：{len(tests)} 项全部复现（说明 5 项 P0 仍存在）")
