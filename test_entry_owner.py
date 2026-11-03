from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

import live_match_loop as live
from core import live_config as lc
from core.paper_models import MarketSnapshot, OrderRequest, OrderSide, OrderStatus, Position
from core.paper_replay import StrategySignal
from core.paper_store import PaperStateStore

CODE, SYMBOL = "301717", "301717.SZ"
OWNER, WATCH = "intraday_t", "sentiment_t"
T0 = datetime(2026, 11, 4, 9, 30)


def config(owner=OWNER, cap=.25):
    cfg = lc.LiveConfig(initial_cash=300000, cross_symbol_cap=cap,
                        risk={"max_order_value_pct": .2, "max_symbol_position_pct": .25},
                        positions=[lc.LivePosition(CODE, OWNER), lc.LivePosition(CODE, WATCH)])
    if owner:
        cfg.entry_owner_by_code = {CODE: owner}
    return cfg


def signal(sid, side, at):
    return StrategySignal(sid, SYMBOL, side, 100, at, "测试")


def test_entry_owner_roundtrip_rejects_disabled_or_invalid():
    cfg = config()
    assert lc.from_dict(cfg.to_dict()).entry_owner_by_code == {CODE: OWNER}
    assert lc.from_dict({}).entry_owner_by_code == {}
    cfg.positions[0].enabled = False
    assert lc.validate(cfg)
    with pytest.raises(ValueError, match="已启用"):
        lc.from_dict(cfg.to_dict())
    with pytest.raises(ValueError, match="映射"):
        lc.from_dict({"entry_owner_by_code": []})


@pytest.mark.parametrize("reverse", [False, True])
def test_observer_old_buy_releases_quota_before_owner_and_keeps_exit(tmp_path, monkeypatch, reverse):
    cfg = config()
    before = T0 - timedelta(minutes=1)
    obs = {}
    for sid in (OWNER, WATCH):
        b = live._broker("2026-11-04", cfg)

        def handler(bar, broker, sid=sid):
            ts = bar["time"].to_pydatetime()
            if ts == T0 and sid == WATCH:
                return [signal(sid, OrderSide.BUY, ts), signal(sid, OrderSide.SELL, ts)]
            return []

        obs[sid] = live.Observer(CODE, sid, SYMBOL, {}, broker=b, cfg=cfg,
                                engine=SimpleNamespace(on_bar=handler),
                                store=PaperStateStore(tmp_path / sid))
    owner = obs[OWNER].broker
    watch = obs[WATCH].broker
    # 40k 的旧买单加 35k 旧持仓已占满额度，执行者不能在它撤销前过闸。
    watch.account.positions[SYMBOL] = Position(SYMBOL, quantity=100, sellable_quantity=100,
                                               average_cost=350, market_price=350)
    watch.account.cash -= 35000
    watch.submit_order(OrderRequest("watch-buy", SYMBOL, OrderSide.BUY, 100, before),
                       MarketSnapshot(SYMBOL, before, 400, 10000), before,
                       execute_immediately=False, execution_wait_bars=5)
    owner.submit_order(OrderRequest("owner-buy", SYMBOL, OrderSide.BUY, 100, before),
                       MarketSnapshot(SYMBOL, before, 350, 10000), before,
                       execute_immediately=False)
    rows = pd.DataFrame({"time": [T0, T0 + timedelta(minutes=1)], "open": 350.0,
                         "close": 350.0, "volume": 10000})
    monkeypatch.setattr(live, "_load_live", lambda _: rows)
    monkeypatch.setattr(live, "_with_price_limits", lambda full, new, symbol: new)
    order = list(obs.values())
    live._run_once(order[::-1] if reverse else order, tmp_path)
    assert watch.orders["watch-buy"].status == OrderStatus.CANCELLED
    assert owner.orders["owner-buy"].status == OrderStatus.FILLED
    assert [f.side for f in watch.fills] == [OrderSide.SELL]
    assert watch.account.positions[SYMBOL].quantity == 0
    assert [s["order_status"] for s in obs[WATCH].signals] == ["OBSERVE_ONLY", "SUBMITTED"]
    restored = obs[WATCH].store.load()
    assert restored.orders["watch-buy"].status == OrderStatus.CANCELLED
    assert len(restored.fills) == 1
    monkeypatch.setattr(live, "_base_signals", lambda _: pd.DataFrame())
    metrics = live._compare_signals(obs[WATCH])
    assert metrics["observed_buy_signals"] == 1
    # The old executable buy remains in the audit; the new observe-only buy does not.
    assert metrics["execution_loss"] == .5


def test_gate_applies_without_portfolio_cap_and_legacy_is_open():
    b = live._broker("2026-11-04", config())
    obs = live.Observer(CODE, WATCH, SYMBOL, {}, broker=b, cfg=config(cap=None))
    assert not live._gate_for(obs, None)(signal(WATCH, OrderSide.BUY, T0), 255, 0)
    obs.cfg = config(owner=None, cap=None)
    assert live._gate_for(obs, None) is None
