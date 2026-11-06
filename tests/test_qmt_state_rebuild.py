"""Offline acceptance for 300319; no QMT, login or order submission."""
import ast
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.intraday_t_broker import filled_inventory
from core.paper_models import Fill, OrderSide
from qmt_builtin.state_rebuild import rebuild_state, sell_allocations


SYMBOL = "300319.SZ"
OWNER = "intraday_t"
AS_OF = "2026-11-06T10:00:00+08:00"
ANCHOR_TIME = "2026-11-05T16:00:00+08:00"


def risk_row(equity=300000.0, **changes):
    row = dict(verified=True, history_complete=True, symbol=SYMBOL, strategy_id=OWNER,
               as_of=AS_OF, trading_day="2026-11-06", equity=equity,
               day_open_equity=300000.0, high_water_equity=max(300000.0, equity),
               daily_loss_latched=False, drawdown_latched=False)
    row.update(changes)
    return row


@pytest.fixture
def snapshot():
    return dict(
        account={"available": 500000.0, "total_equity": 800000.0},
        positions=[dict(symbol=SYMBOL, quantity=200, sellable_quantity=200)],
        orders=[], deals=[], as_of=AS_OF, mark_price=15.0,
        anchor=dict(verified=True, symbol=SYMBOL, strategy_id=OWNER,
                    as_of=ANCHOR_TIME, cash=297000.0, realized_pnl=0.0,
                    external_quantity=0,
                    lots=[dict(quantity=100, cost=15.0, kind="BASE", bought_on="2026-11-04"),
                          dict(quantity=100, cost=15.0, kind="T", bought_on="2026-11-05")]),
        coverage=dict(verified=True, deals_complete=True, orders_complete=True,
                      positions_complete=True, fees_complete=True, ownership_verified=True,
                      corporate_actions_clear=True, submissions_reconciled=True,
                      **{"from": ANCHOR_TIME, "to": AS_OF}),
        risk=risk_row(),
    )


def order(order_id, side="BUY", *, quantity=100, filled=100, status="filled",
          owner=OWNER, reason="加权买入", at="2026-11-06T09:40:00+08:00"):
    return dict(order_id=order_id, symbol=SYMBOL, owner=owner, side=side,
                quantity=quantity, filled_quantity=filled, status=status,
                reason=reason, time=at, limit_price=16.0, fee_reserve=2.0)


def deal(trade_id, order_id, *, side="BUY", quantity=100, price=15.0, fee=1.0,
         at="2026-11-06T09:41:00+08:00"):
    return dict(trade_id=trade_id, order_id=order_id, symbol=SYMBOL, side=side,
                quantity=quantity, price=price, fee=fee, time=at)


def assert_halted(state):
    assert state["mode"] == "halt"
    assert not state["can_open"] and not state["can_reduce"]
    assert state["max_open_value"] == 0
    assert state["blockers"]


def test_overnight_t_is_owned_inventory_not_yesterday_position(snapshot):
    snapshot["positions"][0]["yesterday_quantity"] = 200
    result = rebuild_state(**snapshot)
    assert result["mode"] == "ready"
    assert result["base_shares"] == result["t_total_shares"] == 100
    assert result["t_avg_cost"] == 15.0
    assert result["sellable_t"] == 100


def test_restart_is_repeatable_and_does_not_mutate_broker_evidence(snapshot):
    original = deepcopy(snapshot)
    first = rebuild_state(**snapshot)
    assert first == rebuild_state(**deepcopy(snapshot))
    assert snapshot == original


def test_disordered_anchor_cannot_silently_change_fifo_costs(snapshot):
    snapshot["anchor"]["lots"].reverse()
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["blockers"] == ["anchor_fifo_order_unverified"]


@pytest.mark.parametrize("available,allowed", [(209999.99, False), (210000.0, True)])
def test_real_cash_floor_and_single_order_limit(snapshot, available, allowed):
    snapshot["account"]["available"] = available
    result = rebuild_state(**snapshot)
    assert result["can_open"] is allowed
    assert result["max_open_value"] == (60000 if allowed else 0)


def test_symbol_cap_uses_current_exposure(snapshot):
    snapshot["mark_price"] = 1050.0
    snapshot["risk"] = risk_row(507000.0)
    result = rebuild_state(**snapshot)
    assert result["mode"] == "reduce_only"
    assert result["blockers"] == ["no_open_capacity"]


def test_next_trading_day_preserves_inventory_and_cost(snapshot):
    first = rebuild_state(**snapshot)
    snapshot["as_of"] = "2026-11-09T10:00:00+08:00"
    snapshot["coverage"]["to"] = snapshot["as_of"]
    snapshot["risk"].update(as_of=snapshot["as_of"], trading_day="2026-11-09")
    second = rebuild_state(**snapshot)
    assert second["t_buy_queue"] == first["t_buy_queue"]
    assert second["sellable_t"] == 100


def test_buy_fees_and_t_plus_one_survive_next_day_restart(snapshot):
    snapshot["orders"] = [order("buy")]
    snapshot["deals"] = [deal("fill", "buy")]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=200)
    snapshot["risk"] = risk_row(299999.0)
    before = rebuild_state(**snapshot)
    assert before["t_total_shares"] == 200
    assert before["sellable_t"] == 100
    assert before["total_pnl"] == -1
    snapshot["as_of"] = "2026-11-09T10:00:00+08:00"
    snapshot["coverage"]["to"] = snapshot["as_of"]
    snapshot["risk"].update(as_of=snapshot["as_of"], trading_day="2026-11-09",
                            day_open_equity=299999.0)
    snapshot["positions"][0]["sellable_quantity"] = 300
    after = rebuild_state(**snapshot)
    assert after["t_buy_queue"] == before["t_buy_queue"]
    assert after["sellable_t"] == 200


def test_partial_buy_reserves_only_unfilled_amount_plus_fee(snapshot):
    snapshot["orders"] = [order("partial", quantity=200, filled=100, status="partial")]
    snapshot["deals"] = [deal("fill", "partial", price=16)]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=200)
    snapshot["risk"] = risk_row(299899.0)
    result = rebuild_state(**snapshot)
    assert result["strategy_cash"] == 295399
    assert result["reserved_buy_cash"] == 1602
    assert result["pending_exposure"] == 1600
    assert result["available_strategy_cash"] == 293797
    assert result["t_total_shares"] == 200
    assert_halted(result)
    assert result["blockers"] == ["pending_orders"]


def test_partial_sell_does_not_double_subtract_broker_freeze(snapshot):
    snapshot["orders"] = [order("sell", "SELL", quantity=50, filled=20, status="partial")]
    snapshot["deals"] = [deal("sale", "sell", side="SELL", quantity=20, price=16)]
    snapshot["positions"][0].update(quantity=180, sellable_quantity=150)
    snapshot["risk"] = risk_row(300019.0)
    result = rebuild_state(**snapshot)
    assert result["reserved_sell_quantity"] == 30
    assert result["sellable_t"] == 50
    assert result["today_realized_pnl"] == 19
    assert_halted(result)


def test_duplicate_deal_callbacks_are_idempotent(snapshot):
    snapshot["orders"] = [order("buy")]
    snapshot["deals"] = [deal("fill", "buy")]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=200)
    snapshot["risk"] = risk_row(299999.0)
    once = rebuild_state(**snapshot)
    snapshot["deals"].append(deepcopy(snapshot["deals"][0]))
    assert rebuild_state(**snapshot) == once


def test_conflicting_duplicate_deals_fail_instead_of_choosing_one(snapshot):
    snapshot["deals"] = [deal("fill", "buy"), deal("fill", "buy", price=16)]
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["blockers"] == ["conflicting_trade_id"]


def test_fees_realized_and_unrealized_profit_reconcile(snapshot):
    snapshot["orders"] = [order("sell", "SELL"), order("buy")]
    snapshot["deals"] = [deal("sold", "sell", side="SELL", price=14),
                         deal("bought", "buy")]
    snapshot["positions"][0]["sellable_quantity"] = 100
    snapshot["risk"] = risk_row(299898.0)
    result = rebuild_state(**snapshot)
    assert result["mode"] == "ready"
    assert result["base_shares"] == 100
    assert result["t_total_shares"] == 100
    assert result["sellable_t"] == 0
    assert result["today_realized_pnl"] == -101
    assert result["unrealized_pnl"] == -1
    assert result["total_pnl"] == -102


def test_sell_cannot_consume_base_or_today_t_inventory(snapshot):
    snapshot["orders"] = [order("oversell", "SELL", quantity=200, filled=200)]
    snapshot["deals"] = [deal("sale", "oversell", side="SELL", quantity=200)]
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["blockers"] == ["sale_exceeds_owned_sellable_t"]


def test_same_day_position_does_not_override_t_plus_one(snapshot):
    snapshot["anchor"]["as_of"] = "2026-11-06T09:00:00+08:00"
    snapshot["anchor"]["lots"][1]["bought_on"] = "2026-11-06"
    snapshot["coverage"]["from"] = snapshot["anchor"]["as_of"]
    assert_halted(rebuild_state(**snapshot))


def test_verified_manual_round_trip_does_not_enter_strategy_pnl(snapshot):
    snapshot["anchor"]["external_quantity"] = 100
    snapshot["positions"][0].update(quantity=300, sellable_quantity=200)
    snapshot["orders"] = [order("manual_buy", owner="manual"), order("manual_sell", "SELL", owner="manual")]
    snapshot["deals"] = [deal("buy", "manual_buy"), deal("sell", "manual_sell", side="SELL")]
    result = rebuild_state(**snapshot)
    assert result["mode"] == "ready"
    assert result["external_quantity"] == 100
    assert result["external_bought_today"] == 100
    assert result["t_total_shares"] == 100
    assert result["total_pnl"] == result["realized_pnl"] == 0


def test_foreign_filled_order_after_anchor_cannot_hide_in_incomplete_deals(snapshot):
    snapshot["orders"] = [order("manual", owner="manual")]
    assert_halted(rebuild_state(**snapshot))


def test_other_holdings_and_account_total_equity_do_not_change_strategy_pnl(snapshot):
    first = rebuild_state(**snapshot)
    snapshot["account"]["total_equity"] = 8000000
    snapshot["positions"].append(dict(symbol="600000.SH", quantity=10000, sellable_quantity=10000))
    snapshot["orders"] = [dict(order_id="outside", symbol="600000.SH")]
    snapshot["deals"] = [dict(trade_id="outside", symbol="600000.SH")]
    assert rebuild_state(**snapshot) == first


def test_reverse_repo_receivables_do_not_increase_actual_available_cash(snapshot):
    snapshot["account"].update(available=500.0, repurchase_value=500000.0)
    result = rebuild_state(**snapshot)
    assert not result["can_open"] and result["can_reduce"]
    assert "account_cash_floor" in result["blockers"]
    assert result["available_strategy_cash"] == 500


def test_daily_loss_limit_is_reduce_only(snapshot):
    snapshot["risk"] = risk_row(day_open_equity=303000.0, high_water_equity=303000.0)
    result = rebuild_state(**snapshot)
    assert result["mode"] == "reduce_only" and result["can_reduce"]
    assert result["day_pnl"] == -3000
    assert result["daily_halt"] and not result["can_open"]


def test_daily_halt_survives_recovery_and_restart(snapshot):
    snapshot["risk"] = risk_row(day_open_equity=299000.0, daily_loss_latched=True)
    result = rebuild_state(**snapshot)
    assert result["day_pnl"] == 1000
    assert result["daily_halt"] and not result["can_open"]
    assert result == rebuild_state(**deepcopy(snapshot))


@pytest.mark.parametrize("peak,latched", [(315000.0, False), (305000.0, True)])
def test_drawdown_limit_and_latch_halt_all_decisions(snapshot, peak, latched):
    snapshot["risk"] = risk_row(high_water_equity=peak, drawdown_latched=latched)
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["blockers"] == ["drawdown_halt"]


@pytest.mark.parametrize("field", ["day_open_equity", "high_water_equity", "daily_loss_latched", "drawdown_latched"])
def test_missing_restart_risk_evidence_cannot_reset_limits(snapshot, field):
    del snapshot["risk"][field]
    assert_halted(rebuild_state(**snapshot))


def test_stale_risk_checkpoint_is_not_used_for_new_day(snapshot):
    snapshot["risk"]["trading_day"] = "2026-11-05"
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["blockers"] == ["risk_checkpoint_stale"]


@pytest.mark.parametrize("field", ["verified", "deals_complete", "orders_complete", "positions_complete",
                                 "fees_complete", "ownership_verified", "corporate_actions_clear", "submissions_reconciled"])
def test_incomplete_evidence_stops_decisions(snapshot, field):
    snapshot["coverage"][field] = False
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["t_total_shares"] is None


def test_unknown_inventory_is_not_guessed_from_yesterday_quantity(snapshot):
    snapshot["anchor"] = None
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["t_total_shares"] is None


@pytest.mark.parametrize("owner", [None, ""])
def test_missing_strategy_identity_cannot_be_verified_by_matching_empty_fields(snapshot, owner):
    snapshot["strategy_id"] = owner
    snapshot["anchor"]["strategy_id"] = owner
    snapshot["risk"]["strategy_id"] = owner
    assert_halted(rebuild_state(**snapshot))


@pytest.mark.parametrize("status", ["unknown", "pending_submit", 56])
def test_unverified_qmt_status_and_unresolved_submission_fail_closed(snapshot, status):
    snapshot["orders"] = [order("uncertain", filled=0, status=status)]
    assert_halted(rebuild_state(**snapshot))


def test_position_mismatch_blocks_even_when_account_has_enough_cash(snapshot):
    snapshot["positions"][0]["quantity"] = 300
    result = rebuild_state(**snapshot)
    assert_halted(result)
    assert result["blockers"] == ["position_reconciliation_failed"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.7976931348623157e308, -1])
def test_invalid_prices_and_sentinels_do_not_become_costs(snapshot, bad):
    snapshot["anchor"]["lots"][1]["cost"] = bad
    assert_halted(rebuild_state(**snapshot))


def test_partial_order_at_anchor_needs_pre_anchor_fill_watermark(snapshot):
    snapshot["anchor"]["lots"][1]["quantity"] = 50
    snapshot["anchor"]["cash"] = 297750.0
    snapshot["anchor"]["order_fills"] = {"old": 50}
    snapshot["orders"] = [order("old", at="2026-11-05T14:00:00+08:00")]
    snapshot["deals"] = [deal("rest", "old", quantity=50, fee=0)]
    snapshot["positions"][0]["sellable_quantity"] = 150
    result = rebuild_state(**snapshot)
    assert result["mode"] == "ready" and result["sellable_t"] == 50
    del snapshot["anchor"]["order_fills"]
    assert_halted(rebuild_state(**snapshot))


def test_replay_matches_existing_broker_driven_fifo_and_costs(snapshot):
    snapshot["anchor"].update(as_of="2026-11-04T00:00:00+08:00", cash=300000.0, lots=[])
    snapshot["coverage"]["from"] = snapshot["anchor"]["as_of"]
    snapshot["orders"] = [order("base", reason="建底仓", at="2026-11-04T09:30:00+08:00"),
                          order("t", at="2026-11-05T09:30:00+08:00"),
                          order("sell", "SELL", quantity=50, filled=50)]
    snapshot["deals"] = [deal("base_fill", "base", at="2026-11-04T09:31:00+08:00"),
                         deal("t_fill", "t", price=16, at="2026-11-05T09:31:00+08:00"),
                         deal("sale", "sell", side="SELL", quantity=50, price=17)]
    snapshot["positions"][0].update(quantity=150, sellable_quantity=150)
    snapshot["mark_price"] = 17.0
    snapshot["risk"] = risk_row(300297.0)
    broker = SimpleNamespace(
        fills=[Fill(row["trade_id"], row["order_id"], SYMBOL, OrderSide(row["side"]),
                    row["quantity"], row["price"], row["fee"], datetime.fromisoformat(row["time"]))
               for row in snapshot["deals"]],
        orders={row["order_id"]: SimpleNamespace(request=SimpleNamespace(reason=row["reason"]))
                for row in snapshot["orders"]}, corporate_action_events=[])
    _, old_lots, _ = filled_inventory(broker, SYMBOL, 100)
    result = rebuild_state(**snapshot)
    assert result["mode"] == "ready"
    assert [(lot.cost, lot.quantity, lot.protected, lot.bought_on) for lot in old_lots] == [
        (lot["cost"], lot["quantity"], lot["kind"] == "BASE", lot["bought_on"])
        for lot in result["lots"]]
    assert result["today_realized_pnl"] == pytest.approx(48.5)
    assert result["total_pnl"] == pytest.approx(297)


def test_shared_fifo_preserves_base_fallback_for_existing_broker_only():
    lots = [(100, True, "2026-11-04"), (100, False, "2026-11-05"), (100, False, "2026-11-06")]
    assert sell_allocations(lots, 150, "2026-11-06") == ([50, 100, 0], 0)
    assert sell_allocations(lots, 150, "2026-11-06", include_base=False) == ([0, 100, 0], 50)


def test_sandbox_module_is_ascii_and_has_no_io_or_qmt_entrypoints():
    path = Path(__file__).resolve().parents[1] / "qmt_builtin" / "state_rebuild.py"
    raw = path.read_bytes()
    assert all(byte < 128 for byte in raw)
    tree = ast.parse(raw)
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert imports == {"math", "datetime"}
    forbidden = {"open", "passorder", "cancel", "print", "get_trade_detail_data"}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in forbidden for node in ast.walk(tree))
