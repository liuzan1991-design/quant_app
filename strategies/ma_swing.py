# -*- coding: utf-8 -*-
"""均线波段策略：日线趋势过滤，回踩均线介入，ATR/均线/持仓期退出。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.base import BacktestResult, BaseStrategy, ParamMeta
from core.adjustment import prepare_signal_prices
from core.ma_swing_signals import calculate_order_shares, shifted_execution_signals

ENGINE_DIR = Path(__file__).resolve().parents[2]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
import backtest_generic as bg  # noqa: E402


class MaSwingStrategy(BaseStrategy):
    id = "ma_swing"
    name = "均线波段（回踩趋势）"
    description = "日线多头趋势中回踩MA10/MA20买入，趋势破坏或ATR移动止损退出"

    param_meta = [
        ParamMeta("fast_ma", "快线周期", 5, "number", 2, 30, 1),
        ParamMeta("pullback_ma", "回踩均线周期", 10, "number", 3, 60, 1),
        ParamMeta("trend_ma", "趋势均线周期", 20, "number", 5, 120, 1),
        ParamMeta("slow_ma", "慢线周期", 60, "number", 20, 250, 1),
        ParamMeta("pullback_tolerance_pct", "回踩容差(%)", 1.5, "number", 0.1, 8.0, 0.1,
                  help="收盘价距离回踩均线不超过该比例，且当日最低价触及均线附近"),
        ParamMeta("rsi_period", "RSI周期", 14, "number", 2, 50, 1),
        ParamMeta("rsi_max", "买入RSI上限", 65.0, "number", 20.0, 90.0, 1.0,
                  help="避免在明显超买位置追高"),
        ParamMeta("breakout_days", "突破周期", 20, "number", 5, 120, 1,
                  help="突破前N日最高价时允许趋势突破入场"),
        ParamMeta("breakout_volume_multiple", "突破量能倍数", 1.3, "number", 0.5, 5.0, 0.1,
                  help="突破日成交量至少达到N日均量的倍数"),
        ParamMeta("strict_alignment", "要求严格多头排列", True, "checkbox",
                  help="要求MA快线 > 回踩线 > 趋势线 > 慢线"),
        ParamMeta("position_pct", "单次仓位比例(%)", 90.0, "number", 5.0, 100.0, 5.0,
                  help="按可用现金计算并向下取整到100股"),
        ParamMeta("atr_period", "ATR周期", 14, "number", 5, 60, 1, group="risk"),
        ParamMeta("atr_stop_multiple", "ATR移动止损倍数", 2.5, "number", 0.5, 8.0, 0.1,
                  group="risk"),
        ParamMeta("max_holding_days", "最大持仓天数", 120, "number", 1, 500, 1,
                  group="risk"),
        ParamMeta("trend_exit_days", "跌破MA20确认天数", 2, "number", 1, 10, 1,
                  group="risk", help="连续N天跌破趋势线再退出，减少假跌破"),
        ParamMeta("commission_rate", "佣金费率", 0.0001, "number", 0.0, 0.01, 0.00001,
                  group="cost"),
        ParamMeta("min_commission", "最低佣金(元)", 0.0, "number", 0.0, 100.0, 0.5,
                  group="cost", help="账户为万一免五时填 0"),
        ParamMeta("stamp_tax_rate", "印花税(卖出)", 0.001, "number", 0.0, 0.01, 0.0001,
                  group="cost"),
        ParamMeta("transfer_fee_rate", "过户费", 0.00002, "number", 0.0, 0.001, 0.00001,
                  group="cost"),
        ParamMeta("slippage_bps", "滑点(bps)", 5, "number", 0, 100, 1,
                  group="cost", help="波段策略默认计5bps成交偏差"),
    ]

    @staticmethod
    def _daily_signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
        """公共日线信号核心；整体后移一日，避免偷看当天收盘。"""
        return shifted_execution_signals(df, p)[
            ["buy_signal", "entry_type", "trend_exit", "atr", "trend_ma"]
        ]

    def run(self, df: pd.DataFrame, init_cash: float, params: dict,
            context: dict = None) -> BacktestResult:
        p = dict(params)
        if df.empty:
            raise ValueError("回测数据为空")
        for col in ("time", "open", "high", "low", "close", "volume"):
            if col not in df.columns:
                raise ValueError(f"缺少行情字段：{col}")

        raw_code = str((context or {}).get("code") or df.attrs.get("code", ""))
        warmup_end = (context or {}).get("warmup_end") or p.pop("warmup_end", None)
        if warmup_end is not None:
            warmup_end = pd.Timestamp(warmup_end)
        prepared, corporate_dates, adjustment = prepare_signal_prices(df, raw_code)
        bars = prepared.sort_values("time").reset_index(drop=True)
        bars["time"] = pd.to_datetime(bars["time"])
        bars["date"] = bars["time"].dt.normalize()
        signals = self._daily_signals(bars, p)
        first_bar = set(bars.groupby("date").head(1).index)

        acc = bg.SimAccount(init_cash, p)
        trades = []
        rejected = []
        equity_curve = []
        entry_date = None
        highest = None
        prev_day = None

        for i, row in bars.iterrows():
            ts = row["time"]
            day = row["date"]
            price = float(row["open"] if i in first_bar else row["close"])

            # 预热段：warmup_end 之前只推进日线状态，不交易。
            if warmup_end is not None and pd.Timestamp(ts) < warmup_end:
                if day != prev_day:
                    acc.new_day()
                    prev_day = day
                equity_curve.append({"time": ts, "equity": acc.equity(float(row["close"])),
                                     "close": float(row["close"]), "cash": acc.cash,
                                     "position": acc.total})
                continue

            if day != prev_day:
                acc.new_day()
                prev_day = day

            sig = signals.loc[day] if day in signals.index else None
            if acc.total > 0:
                highest = max(float(highest or price), float(row["high"]))

            if i in first_bar and sig is not None:
                atr_signal = float(sig["atr"]) if pd.notna(sig["atr"]) else np.nan
                # ATR由前复权信号价计算，退出成交用真实价；必须换算回同一价格尺度。
                signal_close = float(row["signal_close"])
                scale = price / signal_close if signal_close > 0 else 1.0
                atr = atr_signal * scale if pd.notna(atr_signal) else np.nan
                held_days = (int(((signals.index >= entry_date) & (signals.index <= day)).sum())
                             if entry_date is not None else 0)
                atr_stop = (highest - float(p["atr_stop_multiple"]) * atr
                            if highest is not None and pd.notna(atr) else -np.inf)
                exit_reason = None
                if day.date() in corporate_dates:
                    equity_curve.append({"time": ts, "equity": acc.equity(float(row["close"])),
                                         "close": float(row["close"]), "cash": acc.cash,
                                         "position": acc.total})
                    continue
                if acc.total > 0 and price <= atr_stop:
                    exit_reason = "ATR移动止损"
                elif acc.total > 0 and pd.notna(sig["trend_exit"]) and bool(sig["trend_exit"]):
                    exit_reason = "连续跌破趋势线"
                elif acc.total > 0 and held_days >= int(p["max_holding_days"]):
                    exit_reason = "达到最大持仓期"

                if exit_reason:
                    sold = acc.sell(price, acc.total)
                    if sold:
                        trades.append({"time": ts, "direction": "SELL", "price": acc.last_fill_price,
                                       "shares": sold, "reason": exit_reason, "score": "",
                                       "fee": round(acc.last_fee, 2), "t_pnl": None})
                        entry_date = None
                        highest = None
                    else:
                        rejected.append({"time": ts, "direction": "SELL", "price": price,
                                         "shares": acc.total, "reason": "T+1限制，下一交易日再卖"})

                if acc.total == 0 and pd.notna(sig["buy_signal"]) and bool(sig["buy_signal"]):
                    shares = calculate_order_shares(acc.cash, price, p)
                    if shares <= 0:
                        rejected.append({"time": ts, "direction": "BUY", "price": price,
                                         "shares": 0, "reason": "可用资金不足100股"})
                    elif acc.buy(price, shares):
                        trades.append({"time": ts, "direction": "BUY", "price": acc.last_fill_price,
                                       "shares": shares, "reason": str(sig["entry_type"]), "score": "",
                                       "fee": round(acc.last_fee, 2), "t_pnl": None})
                        entry_date = day
                        highest = float(row["high"])
                    else:
                        rejected.append({"time": ts, "direction": "BUY", "price": price,
                                         "shares": shares, "reason": "资金不足（含费用与滑点）"})

            equity_curve.append({"time": ts, "equity": acc.equity(float(row["close"])),
                                 "close": float(row["close"]), "cash": acc.cash,
                                 "position": acc.total})

        acc.rejected = rejected
        bars.attrs["adjustment"] = adjustment
        bars.attrs["corporate_action_dates"] = corporate_dates
        return self._build_result(acc, trades, pd.DataFrame(equity_curve), bars, init_cash, p)

    def _build_result(self, acc, trades, eq, df, init_cash, params) -> BacktestResult:
        cols = ["time", "direction", "price", "shares", "reason", "score", "fee", "t_pnl"]
        trades_df = pd.DataFrame(trades, columns=cols)
        rejected_df = pd.DataFrame(getattr(acc, "rejected", []),
                                   columns=["time", "direction", "price", "shares", "reason"])
        final_equity = float(acc.equity(float(df["close"].iloc[-1])))
        total_return = final_equity / init_cash - 1
        days = int(df["date"].nunique())
        annual = (1 + total_return) ** (252 / max(days, 1)) - 1 if total_return > -1 else -1.0
        peak = eq["equity"].cummax()
        mdd = float(((eq["equity"] - peak) / peak).min())
        calmar = annual / abs(mdd) if mdd < 0 else float("nan")
        buys = trades_df[trades_df["direction"] == "BUY"]
        sells = trades_df[trades_df["direction"] == "SELL"]
        # 波段策略会反复空仓，不能把首笔买入误作“全程持股底仓”；
        # 基准比较由股票区间涨跌幅完成，关闭做T专用的底仓/T盈亏口径。
        base_price = None
        base_shares = 0
        position_value = eq["position"].astype(float) * eq["close"].astype(float)
        equity_safe = eq["equity"].astype(float).replace(0, np.nan)
        exposure = (position_value / equity_safe).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        avg_capital = float(position_value.mean()) if len(position_value) else 0.0
        return BacktestResult(
            strategy_id=self.id, strategy_name=self.name, params=params,
            init_cash=init_cash, equity=eq, trades=trades_df, rejected=rejected_df,
            total_fees=acc.total_fees, final_equity=final_equity,
            total_return=total_return, annual_return=annual, max_drawdown=mdd,
            calmar=calmar, t_sell_count=0, t_win_rate=0.0,
            buy_count=len(buys), sell_count=len(sells), trade_days=days,
            base_price=base_price, base_shares=base_shares,
            avg_exposure=float(exposure.mean()), max_exposure=float(exposure.max()),
            capital_return=((final_equity - init_cash) / avg_capital
                            if avg_capital > 0 else 0.0),
            data_adjustment=str(df.attrs.get("adjustment", "不复权")),
        )
