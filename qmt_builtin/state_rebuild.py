"""Offline, normalized broker-state reconstruction. No QMT calls or IO.

Callers must supply independently verified ownership, history and risk
evidence. Missing evidence never grants permission to trade.
"""

import math
from datetime import date, datetime, timedelta, timezone


CST = timezone(timedelta(hours=8))
INITIAL_EQUITY = 300000.0
SYMBOL_CAP = 210000.0
CASH_FLOOR = 210000.0
ORDER_CAP = 60000.0
DAILY_LOSS_LIMIT = 3000.0
DRAWDOWN_LIMIT = 15000.0
T_BUY_PREFIX = "\u52a0\u6743\u4e70\u5165"
BASE_BUY_REASON = "\u5efa\u5e95\u4ed3"
ACTIVE_STATUSES = ("working", "partial", "pending_submit", "unknown")
FINAL_STATUSES = ("filled", "cancelled", "rejected")


class EvidenceError(ValueError):
    pass


def sell_allocations(lots, quantity, trade_day, include_base=True):
    """FIFO allocation of (quantity, protected, bought_on) tuples.

    Keep broker_driven ordering: sell eligible T lots first, then base
    lots when explicitly allowed. Same-day purchases remain locked.
    """
    allocations = [0] * len(lots)
    remaining = quantity
    for protected in ((False, True) if include_base else (False,)):
        for index, (shares, is_base, bought_on) in enumerate(lots):
            if is_base == protected and bought_on < trade_day and remaining:
                taken = min(shares, remaining)
                allocations[index] = taken
                remaining -= taken
    return allocations, remaining


def _require(condition, reason):
    if not condition:
        raise EvidenceError(reason)


def _money(value, name, minimum=None):
    _require(not isinstance(value, bool), "invalid_" + name)
    number = float(value)
    _require(math.isfinite(number) and abs(number) < 1e12, "invalid_" + name)
    _require(minimum is None or number >= minimum, "invalid_" + name)
    return number


def _quantity(value, name):
    number = _money(value, name, 0)
    _require(number.is_integer(), "invalid_" + name)
    return int(number)


def _owner(order):
    owner = order.get("owner")
    _require(isinstance(owner, str) and bool(owner.strip()) and owner == owner.strip()
             and owner.strip().lower() not in ("unknown", "unassigned", "none"), "order_owner_unverified")
    return owner


def _timestamp(value):
    result = datetime.fromisoformat(value)
    _require(result.tzinfo is not None, "timezone_required")
    return result.astimezone(CST)


def _unique(rows, identity):
    result = {}
    for row in rows:
        key = row[identity]
        _require(isinstance(key, str) and bool(key), "missing_" + identity)
        _require(key not in result or row == result[key], "conflicting_" + identity)
        result[key] = row
    return result


def _anchor(anchor, symbol, strategy_id, at):
    _require(isinstance(anchor, dict) and anchor.get("verified") is True, "anchor_unverified")
    _require(anchor["symbol"] == symbol and anchor["strategy_id"] == strategy_id, "anchor_scope_mismatch")
    cutoff = _timestamp(anchor["as_of"])
    _require(cutoff <= at, "anchor_in_future")
    cash = _money(anchor["cash"], "anchor_cash", 0)
    realized = _money(anchor["realized_pnl"], "anchor_realized_pnl")
    lots = []
    for lot in anchor["lots"]:
        quantity = _quantity(lot["quantity"], "lot_quantity")
        cost = _money(lot["cost"], "lot_cost", 0)
        day = date.fromisoformat(lot["bought_on"]).isoformat()
        _require(quantity > 0 and cost > 0 and day <= cutoff.date().isoformat(), "invalid_anchor_lot")
        _require(lot["kind"] in ("BASE", "T"), "unknown_lot_kind")
        lots.append(dict(quantity=quantity, cost=cost, kind=lot["kind"], bought_on=day))
    days = [lot["bought_on"] for lot in lots]
    _require(days == sorted(days), "anchor_fifo_order_unverified")
    _require(abs(cash + sum(lot["quantity"] * lot["cost"] for lot in lots)
                 - INITIAL_EQUITY - realized) <= 0.01, "anchor_cash_cost_mismatch")
    external = _quantity(anchor["external_quantity"], "external_quantity")
    if external and cutoff.date() == at.date():
        _require("external_bought_today" in anchor, "external_today_quantity_unverified")
    external_today = _quantity(anchor.get("external_bought_today", 0), "external_bought_today")
    _require(external_today <= external, "external_today_quantity_invalid")
    return cutoff, cash, realized, lots, external, external_today


def _coverage(coverage, cutoff, at):
    _require(isinstance(coverage, dict), "coverage_missing")
    for field in ("verified", "deals_complete", "orders_complete", "positions_complete",
                  "fees_complete", "ownership_verified", "corporate_actions_clear",
                  "submissions_reconciled"):
        _require(coverage.get(field) is True, "coverage_" + field)
    _require(_timestamp(coverage["from"]) == cutoff and _timestamp(coverage["to"]) == at,
             "coverage_interval_mismatch")


def _replay(deals, order_map, anchor, cutoff, at, symbol, strategy_id, cash, realized, lots,
            external, external_today):
    today_realized = 0.0
    fills_by_order = {}
    selected = []
    for deal in _unique(deals, "trade_id").values():
        if deal["symbol"] != symbol:
            continue
        filled_at = _timestamp(deal["time"])
        if filled_at <= cutoff:
            continue
        _require(filled_at <= at, "future_deal")
        order = order_map.get(deal["order_id"])
        _require(order is not None, "orphan_deal")
        _require(order["symbol"] == symbol, "deal_order_symbol_mismatch")
        _owner(order)
        _require(deal["side"] == order["side"], "deal_order_side_mismatch")
        _require(deal["side"] in ("BUY", "SELL"), "unknown_side")
        quantity = _quantity(deal["quantity"], "deal_quantity")
        price = _money(deal["price"], "deal_price", 0)
        fee = _money(deal["fee"], "deal_fee", 0) if order["owner"] == strategy_id else None
        _require(quantity > 0 and price > 0, "empty_deal")
        selected.append((filled_at, deal["trade_id"], deal, order, quantity, price, fee))

    external_day = cutoff.date().isoformat()
    for filled_at, _, deal, order, quantity, price, fee in sorted(selected, key=lambda item: item[:2]):
        day = filled_at.date().isoformat()
        if day != external_day:
            external_today = 0
            external_day = day
        fills_by_order[deal["order_id"]] = fills_by_order.get(deal["order_id"], 0) + quantity
        if order["owner"] != strategy_id:
            if deal["side"] == "BUY":
                external += quantity
                external_today += quantity
            else:
                _require(quantity <= external - external_today, "external_sale_exceeds_sellable_external")
                external -= quantity
            continue
        if deal["side"] == "BUY":
            reason = order["reason"]
            _require(isinstance(reason, str), "unclassified_buy")
            _require(reason == BASE_BUY_REASON or reason.startswith(T_BUY_PREFIX), "unclassified_buy")
            lots.append(dict(quantity=quantity, cost=price + fee / quantity,
                             kind="T" if reason.startswith(T_BUY_PREFIX) else "BASE", bought_on=day))
            cash -= price * quantity + fee
        else:
            _require(deal["side"] == "SELL", "unknown_side")
            allocation, remaining = sell_allocations(
                [(lot["quantity"], lot["kind"] == "BASE", lot["bought_on"]) for lot in lots],
                quantity, day, include_base=False)
            _require(remaining == 0, "sale_exceeds_owned_sellable_t")
            cost = sum(lot["cost"] * taken for lot, taken in zip(lots, allocation))
            for lot, taken in zip(lots, allocation):
                lot["quantity"] -= taken
            lots = [lot for lot in lots if lot["quantity"]]
            profit = price * quantity - fee - cost
            cash += price * quantity - fee
            realized += profit
            if day == at.date().isoformat():
                today_realized += profit
        _require(cash >= -0.01, "negative_strategy_cash")
    if external_day != at.date().isoformat():
        external_today = 0
    return cash, realized, today_realized, lots, fills_by_order, external, external_today


def _pending(order_map, fills_by_order, anchor, cutoff, at, symbol, strategy_id, external, external_today):
    cash = sell_quantity = exposure = 0
    active = []
    external_active = []
    external_sell = 0
    previous_fills = anchor.get("order_fills", {})
    for order_id, order in sorted(order_map.items()):
        if order["symbol"] != symbol:
            continue
        inserted_at = _timestamp(order["time"])
        _require(inserted_at <= at, "future_order")
        status = order["status"]
        _require(status in ACTIVE_STATUSES + FINAL_STATUSES, "unknown_order_status")
        quantity = _quantity(order["quantity"], "order_quantity")
        filled = _quantity(order["filled_quantity"], "filled_quantity")
        _require(quantity > 0 and filled <= quantity, "invalid_order_quantity")
        if inserted_at <= cutoff and order_id not in fills_by_order and status in FINAL_STATUSES:
            continue
        _owner(order)
        _require(order["side"] in ("BUY", "SELL"), "unknown_side")
        if inserted_at > cutoff or order_id in fills_by_order or status in ACTIVE_STATUSES:
            if inserted_at <= cutoff:
                _require(order_id in previous_fills, "anchor_order_fills_missing")
            before = _quantity(previous_fills.get(order_id, 0), "anchor_order_fills")
            _require(before + fills_by_order.get(order_id, 0) == filled, "order_deal_quantity_mismatch")
        _require(status != "filled" or filled == quantity, "filled_order_incomplete")
        _require(status not in ("unknown", "pending_submit"), "submission_unresolved")
        if status not in ACTIVE_STATUSES:
            continue
        remaining = quantity - filled
        _require(remaining > 0, "active_order_without_remaining")
        if order["owner"] != strategy_id:
            external_active.append(order_id)
            if order["side"] == "SELL":
                external_sell += remaining
            continue
        active.append(order_id)
        if order["side"] == "BUY":
            price = _money(order["limit_price"], "pending_price", 0)
            fee = _money(order["fee_reserve"], "fee_reserve", 0)
            _require(price > 0, "pending_price_missing")
            exposure += remaining * price
            cash += remaining * price + fee
        else:
            sell_quantity += remaining
    _require(external_sell <= external - external_today, "external_pending_sale_exceeds_external")
    return cash, sell_quantity, exposure, active, external_active


def _risk(risk, equity, symbol, strategy_id, at, anchor, cutoff, orders):
    _require(isinstance(risk, dict) and risk.get("verified") is True, "risk_history_unverified")
    seed = risk.get("risk_seed", "risk_history_verified")
    _require(seed in ("origin_initialized", "risk_history_verified"), "unknown_risk_seed")
    _require(risk["symbol"] == symbol and risk["strategy_id"] == strategy_id, "risk_scope_mismatch")
    _require(_timestamp(risk["as_of"]) == at and risk["trading_day"] == at.date().isoformat(),
             "risk_checkpoint_stale")
    _require(abs(_money(risk["equity"], "risk_equity") - equity) <= 0.01, "risk_equity_mismatch")
    opening = _money(risk["day_open_equity"], "day_open_equity", 0)
    peak = _money(risk["high_water_equity"], "high_water_equity", INITIAL_EQUITY)
    _require(opening > 0 and peak >= max(equity, opening), "risk_peak_invalid")
    _require(type(risk["daily_loss_latched"]) is bool and type(risk["drawdown_latched"]) is bool,
             "risk_latch_missing")
    if seed == "origin_initialized":
        _require(risk.get("origin_verified") is True, "origin_not_verified")
        _require(_timestamp(risk["strategy_start"]) == at and cutoff == at, "origin_seed_not_at_start")
        _require(not anchor["lots"] and abs(anchor["cash"] - INITIAL_EQUITY) <= 0.01
                 and anchor["realized_pnl"] == 0, "origin_not_zero_strategy_inventory")
        _require(not any(order.get("owner") == strategy_id for order in orders.values()),
                 "origin_has_strategy_orders")
        _require(equity == opening == peak == INITIAL_EQUITY, "origin_equity_invalid")
        _require(not risk["daily_loss_latched"] and not risk["drawdown_latched"], "origin_latch_conflict")
    else:
        _require(risk.get("history_complete") is True, "risk_history_unverified")
    return {
        "risk_seed": seed,
        "risk_history_complete": True,
        "day_pnl": equity - opening,
        "drawdown": peak - equity,
        "daily_halt": risk["daily_loss_latched"] or opening - equity >= DAILY_LOSS_LIMIT,
        "drawdown_halt": risk["drawdown_latched"] or peak - equity >= DRAWDOWN_LIMIT,
    }


def rebuild_state(account, positions, orders, deals, *, as_of, anchor=None,
                  coverage=None, risk=None, mark_price=None,
                  symbol="300319.SZ", strategy_id="intraday_t"):
    """Rebuild a single strategy/symbol from normalized, verified evidence.

    POSITION sellable_quantity and ACCOUNT available are broker values
    already net of broker freezes. No repo receivables are added to cash.
    Returned permissions are offline diagnostics, never an order request.
    """
    state = {"symbol": symbol, "strategy_id": strategy_id, "as_of": as_of,
             "mode": "halt", "can_open": False, "can_reduce": False,
             "blockers": [], "t_total_shares": None, "base_shares": None,
             "sellable_t": 0, "strategy_equity": None, "max_open_value": 0.0}
    try:
        _require(isinstance(symbol, str) and len(symbol) == 9 and symbol[:6].isdigit()
                 and symbol[6:] in (".SZ", ".SH", ".BJ"), "invalid_symbol")
        _require(isinstance(strategy_id, str) and bool(strategy_id.strip()), "strategy_id_missing")
        at = _timestamp(as_of)
        cutoff, cash, realized, lots, external, external_today = _anchor(anchor, symbol, strategy_id, at)
        _coverage(coverage, cutoff, at)
        mark = _money(mark_price, "mark_price", 0)
        _require(mark > 0, "mark_price_missing")
        available = _money(account["available"], "account_available", 0)
        position_map = _unique(positions, "symbol")
        position = position_map.get(symbol, {"quantity": 0, "sellable_quantity": 0})
        total = _quantity(position["quantity"], "position_quantity")
        sellable = _quantity(position["sellable_quantity"], "position_sellable")
        _require(sellable <= total, "position_sellable_invalid")
        order_map = _unique(orders, "order_id")
        cash, realized, today_realized, lots, fills, external, external_today = _replay(
            deals, order_map, anchor, cutoff, at, symbol, strategy_id, cash, realized, lots,
            external, external_today)
        own = sum(lot["quantity"] for lot in lots)
        _require(own + external == total, "position_reconciliation_failed")
        today_bought = sum(lot["quantity"] for lot in lots if lot["bought_on"] == at.date().isoformat())
        _require(sellable <= total - today_bought - external_today, "position_t_plus_one_invalid")
        reserved_cash, reserved_sell, pending_value, active, external_active = _pending(
            order_map, fills, anchor, cutoff, at, symbol, strategy_id, external, external_today)
        t_lots = [lot for lot in lots if lot["kind"] == "T"]
        t_quantity = sum(lot["quantity"] for lot in t_lots)
        eligible = sum(lot["quantity"] for lot in t_lots if lot["bought_on"] < at.date().isoformat())
        _require(reserved_sell <= eligible, "pending_sale_exceeds_sellable_t")
        equity = cash + own * mark
        risk_state = _risk(risk, equity, symbol, strategy_id, at, anchor, cutoff, order_map)
        cost = sum(lot["quantity"] * lot["cost"] for lot in lots)
        state.update(t_total_shares=t_quantity, base_shares=own-t_quantity,
                     t_buy_queue=[(lot["cost"], lot["quantity"]) for lot in t_lots],
                     t_avg_cost=sum(lot["cost"] * lot["quantity"] for lot in t_lots) / t_quantity if t_quantity else 0.0,
                     lots=lots, position_shares=total, strategy_cash=cash,
                     strategy_equity=equity, total_pnl=equity-INITIAL_EQUITY,
                     realized_pnl=realized, today_realized_pnl=today_realized,
                     unrealized_pnl=own*mark-cost, reserved_buy_cash=reserved_cash,
                     external_quantity=external, external_bought_today=external_today,
                     external_pending_order_ids=external_active, symbol_cap_scope="strategy_owned",
                     reserved_sell_quantity=reserved_sell, pending_exposure=pending_value,
                     sellable_t=min(sellable, max(0, eligible-reserved_sell)),
                     available_strategy_cash=max(0.0, min(cash-reserved_cash, available)),
                     pending_order_ids=active, **risk_state)
        blockers = state["blockers"]
        if risk_state["drawdown_halt"]:
            blockers.append("drawdown_halt")
        if active:
            blockers.append("pending_orders")
        if blockers:
            return state
        state["can_reduce"] = state["sellable_t"] > 0
        if risk_state["daily_halt"]:
            blockers.append("daily_loss_halt")
        if available < CASH_FLOOR:
            blockers.append("account_cash_floor")
        room = min(ORDER_CAP, SYMBOL_CAP-own*mark-pending_value, state["available_strategy_cash"])
        if room <= 0:
            blockers.append("no_open_capacity")
        state["can_open"] = not blockers
        state["max_open_value"] = max(0.0, room) if state["can_open"] else 0.0
        state["mode"] = "ready" if state["can_open"] else "reduce_only"
    except EvidenceError as error:
        state["blockers"] = [str(error)]
    except (KeyError, TypeError, ValueError, OverflowError):
        state["blockers"] = ["invalid_or_missing_input"]
    return state
