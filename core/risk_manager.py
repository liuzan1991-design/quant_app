# -*- coding: utf-8 -*-
"""模拟盘事前风控。与策略解耦，只判断订单能否进入本地撮合。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from core.paper_models import (
    AccountState,
    MarketSnapshot,
    OrderRequest,
    OrderSide,
    RiskDecision,
    RiskViolation,
)


@dataclass(frozen=True)
class RiskConfig:
    max_symbol_position_pct: float = 0.15
    max_account_position_pct: float = 0.70
    max_daily_loss_pct: float = 0.01
    max_symbol_cumulative_loss_pct: float = 0.05
    max_quote_delay_seconds: int = 120
    max_order_value_pct: float = 0.15
    stop_loss_streak_limit: int = 3
    pause_trading_days: int = 2
    board_lot: int = 100


class RiskManager:
    def __init__(self, config: Optional[RiskConfig] = None,
                 trading_day_adder: Optional[Callable[[date, int], date]] = None):
        self.config = config or RiskConfig()
        self._trading_day_adder = trading_day_adder or self._add_calendar_days

    @staticmethod
    def _add_calendar_days(start: date, days: int) -> date:
        current = start
        added = 0
        while added < days:
            current += timedelta(days=1)
            if current.weekday() < 5:
                added += 1
        return current

    def evaluate(self, request: OrderRequest, account: AccountState,
                 market: MarketSnapshot, now: datetime) -> RiskDecision:
        violations = []
        cfg = self.config
        equity = account.equity()

        if request.symbol != market.symbol:
            violations.append(RiskViolation("symbol_mismatch", "委托标的与行情标的不一致",
                                            symbol=request.symbol))
        if request.quantity <= 0:
            violations.append(RiskViolation("invalid_quantity", "委托数量必须大于0",
                                            value=request.quantity, symbol=request.symbol))
        elif request.quantity % cfg.board_lot != 0:
            violations.append(RiskViolation("invalid_board_lot", f"买卖数量必须为{cfg.board_lot}股整数倍",
                                            value=request.quantity, limit=cfg.board_lot,
                                            symbol=request.symbol))
        if market.last_price <= 0:
            violations.append(RiskViolation("invalid_price", "行情价格无效",
                                            value=market.last_price, symbol=request.symbol))
        delay = max(0.0, (now - market.timestamp).total_seconds())
        if delay > cfg.max_quote_delay_seconds:
            violations.append(RiskViolation("stale_quote", "行情延迟超过限制，禁止新订单",
                                            value=delay, limit=cfg.max_quote_delay_seconds,
                                            symbol=request.symbol))
        if market.is_suspended:
            violations.append(RiskViolation("suspended", "标的处于停牌状态",
                                            symbol=request.symbol))

        if account.daily_realized_pnl <= -account.initial_cash * cfg.max_daily_loss_pct:
            violations.append(RiskViolation("daily_loss_limit", "账户单日亏损达到停机线",
                                            value=account.daily_realized_pnl,
                                            limit=-account.initial_cash * cfg.max_daily_loss_pct))
        symbol_pnl = account.realized_pnl_by_symbol.get(request.symbol, 0.0)
        if symbol_pnl <= -account.initial_cash * cfg.max_symbol_cumulative_loss_pct:
            violations.append(RiskViolation("symbol_loss_limit", "单股累计亏损达到暂停线",
                                            value=symbol_pnl,
                                            limit=-account.initial_cash * cfg.max_symbol_cumulative_loss_pct,
                                            symbol=request.symbol))
        pause_until = account.symbol_pause_until.get(request.symbol)
        if pause_until and date.fromisoformat(account.trading_day) <= date.fromisoformat(pause_until):
            violations.append(RiskViolation("symbol_paused", f"连续止损后暂停至{pause_until}",
                                            symbol=request.symbol))

        position = account.positions.get(request.symbol)
        if request.side == OrderSide.SELL:
            sellable = position.sellable_quantity if position else 0
            if request.quantity > sellable:
                violations.append(RiskViolation("t_plus_one_or_position", "可卖持仓不足，可能受T+1限制",
                                                value=request.quantity, limit=sellable,
                                                symbol=request.symbol))
        elif request.side == OrderSide.BUY and equity > 0 and market.last_price > 0:
            order_value = request.quantity * market.last_price
            projected_symbol = account.position_value(request.symbol) + order_value
            current_total = sum(item.market_value for item in account.positions.values())
            projected_total = current_total + order_value
            if order_value > equity * cfg.max_order_value_pct:
                violations.append(RiskViolation("order_value_limit", "单笔委托金额超过账户比例限制",
                                                value=order_value / equity,
                                                limit=cfg.max_order_value_pct,
                                                symbol=request.symbol))
            if projected_symbol > equity * cfg.max_symbol_position_pct:
                violations.append(RiskViolation("symbol_position_limit", "单股仓位超过限制",
                                                value=projected_symbol / equity,
                                                limit=cfg.max_symbol_position_pct,
                                                symbol=request.symbol))
            if projected_total > equity * cfg.max_account_position_pct:
                violations.append(RiskViolation("account_position_limit", "全账户仓位超过限制",
                                                value=projected_total / equity,
                                                limit=cfg.max_account_position_pct))
            if order_value > account.cash:
                violations.append(RiskViolation("insufficient_cash", "可用资金不足",
                                                value=order_value, limit=account.cash,
                                                symbol=request.symbol))
            if market.limit_up is not None and market.last_price >= market.limit_up:
                violations.append(RiskViolation("limit_up_buy", "涨停状态禁止按最新价模拟买入",
                                                symbol=request.symbol))

        if request.side == OrderSide.SELL and market.limit_down is not None \
                and market.last_price <= market.limit_down:
            violations.append(RiskViolation("limit_down_sell", "跌停状态禁止按最新价模拟卖出",
                                            symbol=request.symbol))
        return RiskDecision(passed=not violations, violations=violations)

    def record_stop_loss(self, account: AccountState, symbol: str, trading_day: date) -> None:
        streak = account.consecutive_stop_losses.get(symbol, 0) + 1
        account.consecutive_stop_losses[symbol] = streak
        if streak >= self.config.stop_loss_streak_limit:
            pause_until = self._trading_day_adder(trading_day, self.config.pause_trading_days)
            account.symbol_pause_until[symbol] = pause_until.isoformat()

    @staticmethod
    def record_profitable_exit(account: AccountState, symbol: str) -> None:
        account.consecutive_stop_losses[symbol] = 0
        account.symbol_pause_until.pop(symbol, None)
