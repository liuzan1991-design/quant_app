# -*- coding: utf-8 -*-
"""日内做T（指标加权）策略：封装 backtest_generic 引擎，遵循统一策略接口。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.base import BacktestResult, BaseStrategy, ParamMeta

# 引擎位于 quant_app 上一级目录
ENGINE_DIR = Path(__file__).resolve().parents[2]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
import backtest_generic as bg  # noqa: E402
from core.adjustment import prepare_signal_prices


class IntradayTStrategy(BaseStrategy):
    id = "intraday_t"
    name = "日内做T（指标加权）"
    description = "MACD/RSI/ATR/九转/VWAP 加权信号，底仓+T仓、T+1、冷却、尾盘平仓"

    param_meta = [
        ParamMeta("base_position", "底仓股数", 1000, "number", 0, 100000, 100,
                  help="首日按区间第一根K线收盘价买入的底仓"),
        ParamMeta("fixed_shares", "单笔交易股数", 100, "number", 0, 100000, 100),
        ParamMeta("sell_threshold", "卖出触发阈值", 7, "slider", 0, 20, 1),
        ParamMeta("buy_threshold", "买入触发阈值", 6, "slider", 0, 20, 1),
        ParamMeta("enable_afternoon_close", "尾盘平仓", True, "checkbox",
                  help="平掉T仓（前一日及更早买入、当日仍持有），保留底仓过夜"),
        ParamMeta("max_position", "最大持仓股数", 6000, "number", 0, 1000000, 100,
                  group="risk"),
        ParamMeta("max_buys_per_day", "每日最大买入次数", 10, "number", 0, 100, 1),
        ParamMeta("max_sells_per_day", "每日最大卖出次数", 10, "number", 0, 100, 1),
        ParamMeta("max_daily_stop_loss", "每日T仓止损次数上限", 1, "number", 0, 20, 1),
        ParamMeta("stop_loss_low", "止损幅度-低波动", 0.013, "number", 0.0, 0.1, 0.001,
                  group="risk"),
        ParamMeta("stop_loss_normal", "止损幅度-中波动", 0.018, "number", 0.0, 0.1, 0.001,
                  group="risk"),
        ParamMeta("stop_loss_high", "止损幅度-高波动", 0.025, "number", 0.0, 0.1, 0.001,
                  group="risk"),
        ParamMeta("commission_rate", "佣金费率", 0.0001, "number", 0.0, 0.01, 0.00001,
                  group="cost"),
        ParamMeta("min_commission", "最低佣金(元)", 0.0, "number", 0.0, 100.0, 0.5,
                  group="cost", help="账户为万一免五时填 0"),
        ParamMeta("stamp_tax_rate", "印花税(卖出)", 0.001, "number", 0.0, 0.01, 0.0001,
                  group="cost"),
        ParamMeta("transfer_fee_rate", "过户费", 0.00002, "number", 0.0, 0.001, 0.00001,
                  group="cost"),
        ParamMeta("slippage_bps", "滑点(bps)", 0, "number", 0, 100, 1,
                  group="cost", help="1 bps=0.01%，买入加价、卖出减价；做T高频建议按实际设 5-10"),
    ]

    def run(self, df: pd.DataFrame, init_cash: float, params: dict,
            context: dict = None) -> BacktestResult:
        p = dict(params)
        # 界面用三档独立输入，引擎需要分档字典
        p["stop_loss_pct"] = {
            "low": p.pop("stop_loss_low"),
            "normal": p.pop("stop_loss_normal"),
            "high": p.pop("stop_loss_high"),
        }
        raw_code = str((context or {}).get("code") or df.attrs.get("code", ""))
        prepared, corporate_dates, adjustment = prepare_signal_prices(df, raw_code)
        prepared.attrs["adjustment"] = adjustment
        prepared.attrs["corporate_action_dates"] = corporate_dates
        acc, trades, eq = bg.run_backtest(prepared, init_cash, p)

        return self._build_result(acc, trades, eq, prepared, init_cash, p)

    def _build_result(self, acc, trades, eq, df, init_cash, params) -> BacktestResult:
        trades_df = pd.DataFrame(trades)
        if len(trades_df):
            trades_df["t_pnl"] = pd.to_numeric(trades_df["t_pnl"], errors="coerce")
            trades_df["score"] = trades_df["score"].fillna("").astype(str)
            trades_df["reason"] = trades_df["reason"].fillna("").astype(str)
            trades_df["fee"] = pd.to_numeric(trades_df["fee"], errors="coerce")
        else:
            trades_df = pd.DataFrame(columns=["time", "direction", "price", "shares",
                                              "reason", "score", "fee", "t_pnl"])
        rejected_df = pd.DataFrame(getattr(acc, "rejected", []))
        if len(rejected_df):
            rejected_df["time"] = pd.to_datetime(rejected_df["time"])
        else:
            rejected_df = pd.DataFrame(columns=["time", "direction", "price", "shares", "reason"])

        final_price = df["close"].iloc[-1]
        final_equity = acc.equity(final_price)
        total_return = final_equity / acc.init_cash - 1
        days = int(pd.to_datetime(df["time"]).dt.date.nunique())
        annual = (1 + total_return) ** (252 / max(days, 1)) - 1
        eq2 = eq.copy()
        peak = eq2["equity"].cummax()
        dd = ((eq2["equity"] - peak) / peak)
        mdd = float(dd.min())
        calmar = annual / abs(mdd) if mdd < 0 else float("nan")

        sells = trades_df[trades_df["direction"] == "SELL"] if len(trades_df) else trades_df
        t_sells = sells[sells["t_pnl"].notna()] if len(sells) else sells
        t_wins = t_sells[t_sells["t_pnl"] > 0] if len(t_sells) else t_sells
        buys = trades_df[trades_df["direction"] == "BUY"] if len(trades_df) else trades_df
        base_price = None
        base_shares = int(params.get("base_position", 0))
        if len(trades_df):
            base_rows = trades_df[trades_df["reason"].astype(str).str.contains("底仓")]
            if len(base_rows):
                base_price = float(base_rows.iloc[0]["price"])

        position_value = eq["position"].astype(float) * eq["close"].astype(float)
        equity_safe = eq["equity"].astype(float).replace(0, np.nan)
        exposure = (position_value / equity_safe).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        avg_capital = float(position_value.mean()) if len(position_value) else 0.0

        return BacktestResult(
            strategy_id=self.id,
            strategy_name=self.name,
            params=params,
            init_cash=init_cash,
            equity=eq,
            trades=trades_df,
            rejected=rejected_df,
            total_fees=acc.total_fees,
            final_equity=final_equity,
            total_return=total_return,
            annual_return=annual,
            max_drawdown=mdd,
            calmar=calmar,
            t_sell_count=len(t_sells),
            t_win_rate=(len(t_wins) / len(t_sells) * 100) if len(t_sells) else 0.0,
            buy_count=len(buys),
            sell_count=len(sells),
            trade_days=days,
            base_price=base_price,
            base_shares=base_shares,
            avg_exposure=float(exposure.mean()),
            max_exposure=float(exposure.max()),
            capital_return=((final_equity - init_cash) / avg_capital
                            if avg_capital > 0 else 0.0),
            data_adjustment=str(df.attrs.get("adjustment", "不复权")),
        )
