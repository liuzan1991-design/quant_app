# -*- coding: utf-8 -*-
"""H77：通过真实 PaperBroker 验收拒单、延迟、部分成交、跨日及恢复。"""
from datetime import datetime
from dataclasses import replace
from types import SimpleNamespace
import json

import numpy as np
import pandas as pd
import pytest

from core.intraday_t_shared_live import IntradayTSharedLiveEngine
from core.sentiment_t_shared_live import SentimentTSharedLiveEngine
from core.intraday_t_shared import IntradayTSharedState
from core.paper_models import MarketSnapshot, OrderRequest, OrderSide, OrderStatus
from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_replay import OfflineReplayEngine
from core.paper_store import PaperStateStore
from core.risk_manager import RiskConfig, RiskManager

SYMBOL = "301717.SZ"


def broker():
    return PaperBroker(300000, "2026-10-30", RiskManager(RiskConfig(
        max_symbol_position_pct=1, max_account_position_pct=1, max_order_value_pct=1,
        max_daily_loss_pct=1, max_symbol_cumulative_loss_pct=1)),
        PaperBrokerConfig(commission_rate=0, min_commission=0, stamp_tax_rate=0,
                          transfer_fee_rate=0, slippage_bps=0, max_volume_participation=.1))


def seed(b, qty=100, price=100, side=OrderSide.BUY, reason="建底仓"):
    now = datetime.fromisoformat(b.account.trading_day + "T10:00:00")
    req = OrderRequest(str(len(b.orders)), SYMBOL, side, qty, now,
                       strategy_id="test", reason=reason)
    return b.submit_order(req, MarketSnapshot(SYMBOL, now, price, 100000), now)


def bar(time="2026-11-02 09:40", price=120, volume=100000):
    return {"time": pd.Timestamp(time), "open": price, "close": price,
            "high": price, "low": price, "volume": volume, "amount": price * volume}


def feed(e, b, rows, gate=None):
    for row in rows:
        ts = row["time"]
        e._indicator[ts] = {"closes": np.full(30, row["close"]), "volumes": np.ones(30),
                            "macd_tail": np.zeros(6), "rsi": 50, "vol_level": "normal",
                            "sig_high": True, "sig_low": True}
        e._vwap[ts] = 100.0
    replay = OfflineReplayEngine(b, gate=gate)
    replay.run(pd.DataFrame(rows), SYMBOL, e.on_bar)
    return replay


@pytest.fixture(params=[IntradayTSharedLiveEngine, SentimentTSharedLiveEngine])
def engine(request, monkeypatch):
    e = request.param(SYMBOL, {"base_position": 100, "fixed_shares": 100,
                              "max_position": 1000, "stop_loss_pct": .018,
                              "sell_threshold": 7, "buy_threshold": 6}, broker_driven=True)
    e._prepared = True
    monkeypatch.setattr(e.engine, "_evaluate_sell", lambda *_: (True, 10, "test"))
    monkeypatch.setattr(e.engine, "_evaluate_buy", lambda *_: (True, 10, "test", {}))
    return e


@pytest.mark.parametrize("cleared", [False, True])
def test_no_ghost_sell_from_base_or_empty_account(engine, cleared):
    b = broker()
    seed(b)
    b.start_new_trading_day("2026-11-02")
    if cleared:
        seed(b, side=OrderSide.SELL, reason="旧卖单")
    # 模拟旧引擎恢复出的错误止损成本，必须被真实成交记录纠正。
    engine.engine.state = IntradayTSharedState(base_ready=True, t_total_shares=50,
                                               t_avg_cost=999, t_buy_queue=[(999, 50)])
    replay = feed(engine, b, [bar()])
    assert replay.signal_log == []
    assert engine.account.broker is b
    assert engine.account.total == (0 if cleared else 100)
    assert engine.engine.state.t_total_shares == 0


@pytest.mark.parametrize("minute,price,reason", [
    ("09:40", 260, "加权卖出"),
    ("09:40", 249, "T仓位止损"),
    ("14:45", 259, "尾盘止盈"),
    ("14:55", 255, "尾盘强制平仓"),
])
def test_h78_first_weighted_buy_stays_t_after_restore(engine, tmp_path, minute, price, reason):
    b = broker()
    b.start_new_trading_day("2026-11-03")
    seed(b, price=255.3276, reason="加权买入[九转低8低9(x10)] 评分=12")
    store = PaperStateStore(tmp_path)
    store.save(b)
    b = store.load()

    # 即使配置了100股底仓目标，首笔加权买入的身份仍是T仓。
    same_day = feed(engine, b, [bar("2026-11-03 14:55", price=249)])
    assert same_day.signal_log == []
    assert engine.engine.state.t_total_shares == 100
    assert engine.engine.state.t_avg_cost == pytest.approx(255.3276)
    assert engine.account.base_position == 0
    assert engine.account.base_target == 100
    assert engine.account.sellable_t == 0

    ts = pd.Timestamp(f"2026-11-04 {minute}")
    eligible = feed(engine, b, [bar(str(ts), price=price)])
    assert eligible.signal_log[0]["reason"].startswith(reason)
    assert eligible.signal_log[0]["quantity"] == 100
    assert eligible.signal_log[0]["filled_quantity"] == 0
    feed(engine, b, [bar(str(ts + pd.Timedelta(minutes=1)), price=price)])
    assert b.fills[-1].side == OrderSide.SELL
    assert b.fills[-1].quantity == 100
    assert engine.engine.state.t_total_shares == 0


def test_h78_today_t_does_not_sell_yesterdays_protected_base(engine):
    b = broker()
    seed(b)
    b.start_new_trading_day("2026-11-02")
    seed(b, price=120, reason="加权买入[试算] 评分=10")
    loss = feed(engine, b, [bar("2026-11-02 10:05", price=95)])
    assert loss.signal_log == []
    assert engine.account.closeable == 100
    assert engine.account.sellable_t == 0
    assert engine.account.base_position == 100
    assert engine.engine.state.t_total_shares == 100


def test_h78_unknown_buy_intent_is_protected(engine):
    b = broker()
    seed(b, reason="")
    later = feed(engine, b, [bar("2026-11-02 14:55")])
    assert later.signal_log == []
    assert engine.account.base_position == 100
    assert engine.engine.state.t_total_shares == 0


def test_h78_fresh_account_builds_and_protects_explicit_base(engine):
    b = broker()
    signal = feed(engine, b, [bar(price=100)])
    assert signal.signal_log[0]["reason"] == "建底仓"
    assert signal.signal_log[0]["quantity"] == 100
    feed(engine, b, [bar("2026-11-02 09:41", price=100)])
    assert engine.account.total == engine.account.base_position == 100
    assert engine.engine.state.t_total_shares == 0


@pytest.mark.parametrize("gate_reject", [True, False])
def test_rejected_buy_never_changes_strategy_cost_or_counters(engine, gate_reject):
    b = broker()
    seed(b)
    b.start_new_trading_day("2026-11-02")
    if not gate_reject:
        b.risk_manager.config = replace(b.risk_manager.config, max_order_value_pct=.001)
    replay = feed(engine, b, [bar(price=90)], gate=(lambda *_: False) if gate_reject else None)
    assert replay.signal_log[0]["order_status"] == "REJECTED"
    assert engine.account.total == 100
    assert engine.engine.state.buy_count_today == 0
    assert engine.engine.state.t_total_shares == 0
    assert engine.engine.state.last_buy_time is None


def test_partial_sell_counts_once_and_preserves_actual_remaining_cost(engine):
    b = broker()
    seed(b)
    seed(b, 200, 110, reason="加权买入[test] 评分=10")
    engine.params["fixed_shares"] = 200
    engine.engine.params["fixed_shares"] = 200
    first = feed(engine, b, [bar(volume=1000)])
    assert first.signal_log[0]["quantity"] == 200
    assert first.signal_log[0]["filled_quantity"] == 0
    assert engine.account.total == 300
    feed(engine, b, [bar("2026-11-02 09:41", volume=1000)])
    assert engine.account.total == 200
    assert engine.engine.state.t_total_shares == 100
    assert engine.engine.state.t_avg_cost == 110
    second = feed(engine, b, [bar("2026-11-02 09:42", volume=1000)])
    assert second.signal_log == []
    assert engine.account.total == 100
    assert engine.engine.state.t_total_shares == 0
    assert engine.engine.state.sell_count_today == 1
    assert len(b.orders) == 3


def test_unfilled_order_waits_for_fill_without_duplicate_sell(engine):
    b = broker()
    seed(b)
    seed(b, 100, 110, reason="加权买入[test] 评分=10")
    first = feed(engine, b, [bar(volume=100)])
    assert first.signal_log[0]["order_status"] == "SUBMITTED"
    assert engine.engine.state.sell_count_today == 0
    assert engine.engine.state.t_total_shares == 100
    second = feed(engine, b, [bar("2026-11-02 09:41", volume=1000)])
    assert second.signal_log == []
    assert engine.account.total == 100
    assert engine.engine.state.sell_count_today == 1
    assert len(b.orders) == 3


def test_stop_loss_survives_day_roll_and_never_sells_base(engine):
    b = broker()
    seed(b)
    seed(b, 200, 110, reason="加权买入[test] 评分=10")
    feed(engine, b, [bar(price=110), bar("2026-11-02 09:41", price=110)])
    # 先前强制卖评分会卖出 100；余下 100 T 股应跨日保留成本。
    assert engine.engine.state.t_total_shares == 100
    replay = feed(engine, b, [bar("2026-11-03 09:40", price=90)])
    assert replay.signal_log[0]["quantity"] == 100
    assert replay.signal_log[0]["reason"].startswith("T仓位止损")
    feed(engine, b, [bar("2026-11-03 09:41", price=90)])
    assert engine.account.total == 100
    assert engine.engine.state.daily_stop_loss_count == 1


def test_restart_with_partial_order_uses_snapshot_and_real_fills(engine, tmp_path):
    b = broker()
    seed(b)
    seed(b, 200, 110, reason="加权买入[test] 评分=10")
    engine.params["fixed_shares"] = engine.engine.params["fixed_shares"] = 200
    feed(engine, b, [bar(volume=1000), bar("2026-11-02 09:41", volume=1000)])
    assert next(reversed(b.orders.values())).filled_quantity == 100
    store = PaperStateStore(tmp_path)
    store.save(b)
    saved_state = engine.engine.state.to_dict()
    restored = store.load()
    new = type(engine)(SYMBOL, engine.params, broker_driven=True)
    new._prepared = True
    new.engine.state = IntradayTSharedState.from_dict(saved_state)
    replay = feed(new, restored, [bar("2026-11-02 09:42", volume=1000)])
    assert replay.signal_log == []
    assert new.account.total == 100
    assert new.engine.state.t_total_shares == 0
    assert new.engine.state.sell_count_today == 1
    assert restored.orders[next(reversed(restored.orders))].request.reason.startswith("加权卖出")


def test_t_plus_one_not_unlocked_by_second_replay_batch():
    b = broker()
    b.start_new_trading_day("2026-11-02")
    seed(b)
    OfflineReplayEngine(b).run(pd.DataFrame([bar()]), SYMBOL, lambda *_: ())
    p = b.account.positions[SYMBOL]
    assert p.today_bought_quantity == 100
    assert p.sellable_quantity == 0


def test_today_buy_does_not_block_sale_of_yesterdays_shares():
    b = broker()
    seed(b)
    b.start_new_trading_day("2026-11-02")
    seed(b, reason="加权买入[test] 评分=10")
    sell = seed(b, side=OrderSide.SELL, reason="卖昨日持仓")
    assert sell.status == OrderStatus.FILLED
    p = b.account.positions[SYMBOL]
    assert (p.quantity, p.sellable_quantity, p.today_bought_quantity) == (100, 0, 100)


def test_signal_recovery_is_append_only_and_keeps_request_time(tmp_path):
    from live_match_loop import Observer
    b = broker()
    seed(b)
    b.start_new_trading_day("2026-11-02")
    now = datetime(2026, 11, 2, 11, 11)
    req = OrderRequest("ghost", SYMBOL, OrderSide.SELL, 50, now, strategy_id="intraday_t")
    b.submit_order(req, MarketSnapshot(SYMBOL, now, 100, 10000), now)
    obs = Observer(code="301717", symbol=SYMBOL, strategy_id="intraday_t", params={})
    obs.broker = b
    obs.signal_path = tmp_path / "signals.jsonl"
    original = json.dumps({"client_order_id": "raw-gate", "order_status": "REJECTED",
                           "reason": "原始拒单", "signal_time": now.isoformat()}) + "\n"
    obs.signal_path.write_text(original, encoding="utf-8")
    assert obs.backfill_signals_from_fills() == 2
    content = obs.signal_path.read_text(encoding="utf-8")
    assert content.startswith(original)
    assert obs.backfill_signals_from_fills() == 0
    assert obs.signal_path.read_text(encoding="utf-8") == content
    recovered = [json.loads(s) for s in content.splitlines()][-1]
    assert recovered["client_order_id"] == "ghost"
    assert recovered["signal_time"] == now.isoformat()
    assert recovered["order_status"] == "REJECTED"


def test_signal_compare_uses_requested_quantity_time_and_rejects(monkeypatch):
    import live_match_loop as lm
    b = broker()
    seed(b)
    order = b.orders["0"]
    obs = SimpleNamespace(broker=b, signals=[{
        "client_order_id": "0", "signal_time": "2026-10-30T10:05:00", "side": "BUY",
        "quantity": 50, "filled_quantity": 0}, {
        "client_order_id": "", "signal_time": "2026-10-30T10:10:00", "side": "BUY",
        "quantity": 100, "filled_quantity": 0, "order_status": "REJECTED"}])
    monkeypatch.setattr(lm, "_base_signals", lambda _: pd.DataFrame([
        {"time": pd.Timestamp(order.request.created_at), "direction": "BUY", "shares": 100},
        {"time": pd.Timestamp("2026-10-30T10:10:00"), "direction": "BUY", "shares": 50}]))
    result = lm._compare_signals(obs)
    assert result["live_signals"] == 2
    assert result["engine_consistency"] == .5
    assert result["execution_loss"] == .5


def test_watchdog_reads_all_enabled_combos(tmp_path, monkeypatch):
    import watchdog
    from core import live_config
    cfg = live_config.from_dict({"positions": [
        {"code": "301717", "strategy_id": "grid_trade"},
        {"code": "301717", "strategy_id": "intraday_t"},
        {"code": "300308", "strategy_id": "grid_trade", "enabled": False}]})
    monkeypatch.setattr(live_config, "load", lambda: cfg)
    monkeypatch.setattr(watchdog, "LIVE_DIR", tmp_path)
    day = watchdog.datetime.now().date().isoformat()
    for sid in ("grid_trade", "intraday_t"):
        root = tmp_path / SYMBOL / sid
        root.mkdir(parents=True)
        (root / "watermark.json").write_text(json.dumps({
            "processed_upto_by_day": {day: day + "T14:59:00"}}), encoding="utf-8")
    assert watchdog._today_watermark_reached_close()
    (tmp_path / SYMBOL / "intraday_t" / "watermark.json").write_text(json.dumps({
        "processed_upto_by_day": {day: day + "T14:58:00"}}), encoding="utf-8")
    assert not watchdog._today_watermark_reached_close()


@pytest.mark.parametrize("sid", ["intraday_t", "sentiment_t"])
def test_production_factory_cannot_create_shadow_account(sid, monkeypatch):
    import live_match_loop as lm
    import backtest_generic as bg
    cls = IntradayTSharedLiveEngine if sid == "intraday_t" else SentimentTSharedLiveEngine
    monkeypatch.setattr(cls, "prepare", lambda self, df: setattr(self, "_prepared", True))
    def forbidden(*_args, **_kwargs):
        raise AssertionError("生产路径创建了 SimAccount")
    monkeypatch.setattr(bg, "SimAccount", forbidden)
    obs = SimpleNamespace(strategy_id=sid, symbol=SYMBOL, code="301717", params={})
    e = lm._engine_for(obs, pd.DataFrame())
    assert e.broker_driven
    assert e.account is None


def test_real_order_reason_roundtrips_and_old_snapshot_remains_readable(tmp_path):
    b = broker()
    seed(b, reason="加权买入[卖出回补(x3)] 评分=10")
    store = PaperStateStore(tmp_path)
    store.save(b)
    assert store.load().orders["0"].request.reason == "加权买入[卖出回补(x3)] 评分=10"
    data = json.loads(store.snapshot_path.read_text(encoding="utf-8"))
    del data["orders"][0]["request"]["reason"]
    store.snapshot_path.write_text(json.dumps(data), encoding="utf-8")
    assert store.load().orders["0"].request.reason == ""


def test_closed_watermark_suppresses_false_stale_alert(tmp_path, monkeypatch):
    import watchdog
    from datetime import timedelta
    now = watchdog.datetime.now()
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps({"date": now.date().isoformat(), "status": "running",
        "updated_at": now.isoformat(),
        "last_success_at": (now - timedelta(seconds=1000)).isoformat()}), encoding="utf-8")
    monkeypatch.setattr(watchdog, "HEARTBEAT", heartbeat)
    monkeypatch.setattr(watchdog, "_in_window", lambda *_: True)
    monkeypatch.setattr(watchdog, "_today_watermark_reached_close", lambda: True)
    monkeypatch.setattr(watchdog, "log", lambda *_: None)
    monkeypatch.setattr(watchdog, "_report_success_staleness", lambda *_: pytest.fail("已收盘不应报陈旧"))
    monkeypatch.setattr(watchdog, "_restart", lambda *_: pytest.fail("已收盘不应重启"))
    assert watchdog.main() == 0


def test_multi_day_mixed_fills_keep_account_and_t_ledger_equal(engine):
    b = broker()
    seed(b)
    rng = np.random.default_rng(20261102)
    for day in pd.date_range("2026-11-02", periods=4, freq="D"):
        for minute in range(20):
            row = bar(str(day + pd.Timedelta(hours=9, minutes=30 + minute * 5)),
                      price=float(rng.choice([90, 100, 120])),
                      volume=int(rng.choice([100, 1000, 10000])))
            feed(engine, b, [row])
            p = b.account.positions[SYMBOL]
            assert p.quantity >= 100
            assert 0 <= p.sellable_quantity <= p.quantity
            assert 0 <= p.today_bought_quantity <= p.quantity
            assert engine.engine.state.t_total_shares == p.quantity - 100
            assert sum(q for _, q in engine.engine.state.t_buy_queue) == p.quantity - 100
    assert all(o.request.quantity % 100 == 0 for o in b.orders.values())
    assert not [a for a in b.audit_log if a.get("event") == "RISK_REJECTED"
                and set(a["violations"]) & {"invalid_board_lot", "t_plus_one_or_position"}]


def test_zero_base_setting_skips_zero_quantity_order_and_respects_t_plus_one(engine):
    engine.params["base_position"] = engine.engine.params["base_position"] = 0
    b = broker()
    first = feed(engine, b, [bar(price=90)])
    assert first.signal_log[0]["quantity"] == 100
    assert first.signal_log[0]["reason"].startswith("加权买入")
    assert engine.engine.state.t_total_shares == 0
    assert feed(engine, b, [bar("2026-11-02 09:41")]).signal_log == []
    assert engine.engine.state.t_total_shares == 100
    next_day = feed(engine, b, [bar("2026-11-03 09:40")])
    assert next_day.signal_log[0]["side"] == "SELL"
    feed(engine, b, [bar("2026-11-03 09:41")])
    assert engine.account.total == 0
