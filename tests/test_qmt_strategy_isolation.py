"""300319 user-selected own-only cap, manual isolation and origin seed acceptance."""
from copy import deepcopy

import pytest

from qmt_builtin.state_rebuild import rebuild_state
from test_qmt_state_rebuild import snapshot, risk_row, order, deal, AS_OF


@pytest.fixture
def origin(snapshot):
    snapshot["strategy_id"] = "new_origin_case"
    snapshot["anchor"].update(strategy_id="new_origin_case", as_of=AS_OF, cash=300000.0,
                              lots=[], external_quantity=2600, external_bought_today=0)
    snapshot["positions"][0].update(quantity=2600, sellable_quantity=2600)
    snapshot["coverage"]["from"] = AS_OF
    snapshot["risk"] = risk_row(strategy_id="new_origin_case", risk_seed="origin_initialized",
                                 origin_verified=True, strategy_start=AS_OF)
    snapshot["risk"].pop("history_complete")
    return snapshot


def external_baseline(snapshot, quantity=100):
    snapshot["anchor"]["external_quantity"] = quantity
    snapshot["positions"][0].update(quantity=200 + quantity, sellable_quantity=200 + quantity)
    return snapshot


def test_new_origin_initializes_empty_history_at_the_actual_start(origin):
    original = deepcopy(origin)
    state = rebuild_state(**origin)
    assert state["mode"] == "ready"
    assert state["risk_seed"] == "origin_initialized" and state["risk_history_complete"]
    assert state["day_pnl"] == state["drawdown"] == state["total_pnl"] == 0
    assert state["external_quantity"] == 2600 and state["t_total_shares"] == state["base_shares"] == 0
    assert not state["can_reduce"]
    assert rebuild_state(**origin) == state and origin == original


def test_origin_does_not_override_actual_cash_floor(origin):
    origin["account"]["available"] = 383.53
    state = rebuild_state(**origin)
    assert state["mode"] == "reduce_only" and state["blockers"] == ["account_cash_floor"]
    assert not state["can_open"] and not state["can_reduce"]


@pytest.mark.parametrize("field", ["origin_verified", "strategy_start"])
def test_origin_requires_evidence_and_start_time(origin, field):
    origin["risk"].pop(field)
    assert rebuild_state(**origin)["mode"] == "halt"


def test_origin_cannot_be_used_after_initialization_time(origin):
    origin["as_of"] = "2026-11-06T10:01:00+08:00"
    origin["coverage"]["to"] = origin["as_of"]
    origin["risk"]["as_of"] = origin["as_of"]
    state = rebuild_state(**origin)
    assert state["mode"] == "halt" and state["blockers"] == ["origin_seed_not_at_start"]


def test_new_origin_cannot_reset_an_existing_inventory(snapshot):
    snapshot["anchor"]["as_of"] = AS_OF
    snapshot["coverage"]["from"] = AS_OF
    snapshot["risk"].update(risk_seed="origin_initialized", origin_verified=True, strategy_start=AS_OF)
    assert rebuild_state(**snapshot)["blockers"] == ["origin_not_zero_strategy_inventory"]


@pytest.mark.parametrize("field", ["daily_loss_latched", "drawdown_latched"])
def test_origin_does_not_clear_existing_latches(origin, field):
    origin["risk"][field] = True
    assert rebuild_state(**origin)["blockers"] == ["origin_latch_conflict"]


def test_origin_cannot_hide_previous_strategy_orders(origin):
    origin["orders"] = [order("old", owner="new_origin_case", at="2026-11-05T10:00:00+08:00")]
    assert rebuild_state(**origin)["blockers"] == ["origin_has_strategy_orders"]


def test_history_mode_still_requires_complete_checkpoints(origin):
    origin["risk"]["risk_seed"] = "risk_history_verified"
    assert rebuild_state(**origin)["blockers"] == ["risk_history_unverified"]
    origin["risk"]["history_complete"] = True
    assert rebuild_state(**origin)["mode"] == "ready"


def test_unknown_risk_seed_is_not_treated_as_a_fresh_start(origin):
    origin["risk"]["risk_seed"] = "reset"
    assert rebuild_state(**origin)["blockers"] == ["unknown_risk_seed"]


def test_strategy_cap_excludes_external_inventory_and_keeps_single_order_limit(origin):
    origin["mark_price"] = 100.0
    state = rebuild_state(**origin)
    assert state["external_quantity"] * 100 > 210000
    assert state["symbol_cap_scope"] == "strategy_owned"
    assert state["max_open_value"] == 60000 and state["can_open"]


def test_strategy_owned_cap_and_pending_buy_are_not_relaxed_by_manual_policy(snapshot):
    snapshot["anchor"].update(lots=[dict(quantity=13000, cost=15.0, kind="T", bought_on="2026-11-05")],
                              cash=105000, external_quantity=2600)
    snapshot["positions"][0].update(quantity=15600, sellable_quantity=15600)
    snapshot["orders"] = [order("own_pending", quantity=200, filled=0, status="working")]
    state = rebuild_state(**snapshot)
    assert state["mode"] == "halt" and state["blockers"] == ["pending_orders"]
    assert state["pending_exposure"] == 3200 and state["reserved_buy_cash"] == 3202


def test_verified_manual_buy_changes_only_external_quantity(snapshot):
    snapshot["orders"] = [order("manual", owner="manual")]
    snapshot["deals"] = [deal("fill", "manual", price=150, fee=None)]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=200)
    state = rebuild_state(**snapshot)
    assert state["mode"] == "ready"
    assert state["strategy_cash"] == 297000 and state["total_pnl"] == 0
    assert state["external_quantity"] == state["external_bought_today"] == 100
    assert state["t_total_shares"] == 100 and state["sellable_t"] == 100


def test_verified_manual_sale_does_not_debit_own_lots_or_realized_pnl(snapshot):
    external_baseline(snapshot, 200)
    snapshot["orders"] = [order("manual_sale", "SELL", owner="manual")]
    snapshot["deals"] = [deal("fill", "manual_sale", side="SELL", price=5, fee=None)]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=300)
    state = rebuild_state(**snapshot)
    assert state["mode"] == "ready" and state["external_quantity"] == 100
    assert state["realized_pnl"] == state["total_pnl"] == 0 and state["t_total_shares"] == 100


def test_manual_round_trip_with_no_external_old_inventory_cannot_sell_strategy_stock(snapshot):
    snapshot["orders"] = [order("a", owner="manual"), order("b", "SELL", owner="manual")]
    snapshot["deals"] = [deal("a-fill", "a"), deal("b-fill", "b", side="SELL")]
    assert rebuild_state(**snapshot)["blockers"] == ["external_sale_exceeds_sellable_external"]


def test_manual_sell_then_buy_cannot_hide_consuming_strategy_inventory(snapshot):
    snapshot["orders"] = [order("a", "SELL", owner="manual"), order("b", owner="manual")]
    snapshot["deals"] = [deal("a-fill", "a", side="SELL"), deal("b-fill", "b")]
    assert rebuild_state(**snapshot)["blockers"] == ["external_sale_exceeds_sellable_external"]


@pytest.mark.parametrize("owner", [None, "", "  ", "UNKNOWN", "unassigned", "None", " intraday_t "])
def test_unknown_owner_is_never_silently_called_manual(snapshot, owner):
    snapshot["orders"] = [order("unknown", owner=owner)]
    snapshot["deals"] = [deal("fill", "unknown")]
    assert rebuild_state(**snapshot)["blockers"] == ["order_owner_unverified"]


def test_missing_external_fill_history_still_halts(snapshot):
    snapshot["orders"] = [order("manual", owner="manual")]
    assert rebuild_state(**snapshot)["blockers"] == ["order_deal_quantity_mismatch"]


def test_manual_pending_buy_is_not_counted_as_strategy_reservation(snapshot):
    snapshot["orders"] = [order("manual_pending", owner="manual", filled=0, status="working")]
    snapshot["account"]["available"] = 240000
    state = rebuild_state(**snapshot)
    assert state["mode"] == "ready" and state["external_pending_order_ids"] == ["manual_pending"]
    assert not state["pending_order_ids"] and state["pending_exposure"] == state["reserved_buy_cash"] == 0
    assert state["available_strategy_cash"] == 240000


def test_manual_pending_buy_can_exhaust_real_account_cash(snapshot):
    snapshot["orders"] = [order("manual_pending", owner="manual", filled=0, status="working")]
    snapshot["account"]["available"] = 200000
    state = rebuild_state(**snapshot)
    assert state["mode"] == "reduce_only" and "account_cash_floor" in state["blockers"]


def test_manual_pending_sale_freeze_is_not_deducted_twice(snapshot):
    external_baseline(snapshot, 100)
    snapshot["orders"] = [order("manual_pending", "SELL", owner="manual", filled=0, status="working")]
    snapshot["positions"][0]["sellable_quantity"] = 200
    state = rebuild_state(**snapshot)
    assert state["mode"] == "ready" and state["sellable_t"] == 100
    assert state["reserved_sell_quantity"] == 0


def test_manual_pending_sale_cannot_reserve_strategy_stock(snapshot):
    snapshot["orders"] = [order("manual_pending", "SELL", owner="manual", filled=0, status="working")]
    assert rebuild_state(**snapshot)["blockers"] == ["external_pending_sale_exceeds_external"]


@pytest.mark.parametrize("status", ["unknown", "pending_submit"])
def test_unknown_external_submission_still_requires_resolution(snapshot, status):
    snapshot["orders"] = [order("manual_pending", owner="manual", filled=0, status=status)]
    assert rebuild_state(**snapshot)["blockers"] == ["submission_unresolved"]


def test_external_same_day_quantities_are_required_at_same_day_anchor(origin):
    origin["anchor"].pop("external_bought_today")
    assert rebuild_state(**origin)["blockers"] == ["external_today_quantity_unverified"]


def test_external_today_buy_does_not_make_account_position_sellable(snapshot):
    snapshot["orders"] = [order("manual", owner="manual")]
    snapshot["deals"] = [deal("fill", "manual")]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=300)
    assert rebuild_state(**snapshot)["blockers"] == ["position_t_plus_one_invalid"]


def test_external_today_lock_expires_next_day_without_changing_own_lots(snapshot):
    snapshot["orders"] = [order("manual", owner="manual")]
    snapshot["deals"] = [deal("fill", "manual")]
    snapshot["as_of"] = "2026-11-09T10:00:00+08:00"
    snapshot["coverage"]["to"] = snapshot["as_of"]
    snapshot["risk"].update(as_of=snapshot["as_of"], trading_day="2026-11-09")
    snapshot["positions"][0].update(quantity=300, sellable_quantity=300)
    state = rebuild_state(**snapshot)
    assert state["mode"] == "ready" and state["external_bought_today"] == 0
    assert state["sellable_t"] == 100


def test_partial_manual_fill_and_cancel_replay_consistently(snapshot):
    snapshot["orders"] = [order("manual", owner="manual", quantity=200, filled=100, status="cancelled")]
    snapshot["deals"] = [deal("fill", "manual", fee=None)]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=200)
    state = rebuild_state(**snapshot)
    assert state["mode"] == "ready" and state["external_quantity"] == 100
    assert state["strategy_cash"] == 297000


def test_duplicate_manual_callback_is_not_counted_twice(snapshot):
    snapshot["orders"] = [order("manual", owner="manual")]
    snapshot["deals"] = [deal("fill", "manual"), deal("fill", "manual")]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=200)
    assert rebuild_state(**snapshot)["external_quantity"] == 100


def test_own_today_stock_remains_locked_with_manual_flow(snapshot):
    snapshot["anchor"].update(as_of="2026-11-06T09:00:00+08:00")
    snapshot["anchor"]["lots"][1]["bought_on"] = "2026-11-06"
    snapshot["coverage"]["from"] = snapshot["anchor"]["as_of"]
    snapshot["orders"] = [order("manual", owner="manual")]
    snapshot["deals"] = [deal("fill", "manual")]
    snapshot["positions"][0].update(quantity=300, sellable_quantity=100)
    state = rebuild_state(**snapshot)
    assert state["mode"] == "ready" and state["sellable_t"] == 0 and not state["can_reduce"]
