# -*- coding: utf-8 -*-
"""网格交易策略：跌一格买、涨回一格卖，机械收割波动。复用引擎 SimAccount（T+1/费用/滑点）。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.base import BacktestResult, BaseStrategy, ParamMeta

ENGINE_DIR = Path(__file__).resolve().parents[2]
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
import backtest_generic as bg  # noqa: E402
from core.adjustment import prepare_signal_prices


class GridTradeStrategy(BaseStrategy):
    id = "grid_trade"
    name = "网格交易"
    description = "移动网格：中枢跟随当日VWAP，每跌一档买一格、涨回一档卖一格"

    param_meta = [
        ParamMeta("base_position", "初始底仓股数", 1000, "number", 0, 100000, 100,
                  help="首日按区间第一根K线收盘价买入，作为网格中枢价"),
        ParamMeta("grid_pct", "网格间距(%)", 2.0, "number", 0.1, 10.0, 0.1,
                  help="每格价格距离，如 2 表示中枢价上下 2% 一格"),
        ParamMeta("grid_levels", "网格层数", 5, "number", 1, 20, 1,
                  help="中枢下方最多买入几格"),
        ParamMeta("trade_shares", "每格买卖股数", 100, "number", 0, 100000, 100),
        ParamMeta("trend_ma", "趋势过滤均线", 60, "number", 20, 250, 1,
                  help="MA向下且股价位于MA下方时暂停新买入"),
        ParamMeta("center_freeze_pct", "中枢最大下移(%)", 8.0, "number", 1.0, 30.0, 0.5,
                  help="网格中枢相对启动价最多下移，避免下跌中不断跟随接货"),
        ParamMeta("hard_stop_pct", "网格硬止损(%)", 12.0, "number", 2.0, 40.0, 0.5,
                  group="risk", help="价格跌破参考中枢该比例后清掉T仓并停止加仓"),
        ParamMeta("max_grid_loss_pct", "网格最大亏损(%)", 5.0, "number", 1.0, 30.0, 0.5,
                  group="risk", help="账户相对期初亏损达到该比例后停止新增网格仓"),
        ParamMeta("enable_afternoon_close", "尾盘平T仓", False, "checkbox",
                  help="14:55 卖出全部T仓（当日及以前买入的，保留底仓）"),
        ParamMeta("max_position", "最大持仓股数", 6000, "number", 0, 1000000, 100,
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
                  group="cost", help="1 bps=0.01%，买入加价、卖出减价"),
    ]

    def run(self, df: pd.DataFrame, init_cash: float, params: dict,
            context: dict = None) -> BacktestResult:
        p = dict(params)
        base_position = int(p.get("base_position", 1000))
        grid_pct = float(p.get("grid_pct", 2.0))
        grid_levels = int(p.get("grid_levels", 5))
        trade_shares = int(p.get("trade_shares", 100))
        max_position = int(p.get("max_position", 6000))
        enable_close = bool(p.get("enable_afternoon_close", False))
        grid_step = grid_pct / 100.0

        raw_code = str((context or {}).get("code") or df.attrs.get("code", ""))
        prepared, corporate_dates, adjustment = prepare_signal_prices(df, raw_code)
        df = prepared.copy()
        df["date"] = pd.to_datetime(df["time"]).dt.date
        closes = df["close"].to_numpy(dtype=np.float64)
        times = df["time"].tolist()
        dates = df["date"].to_numpy()
        hhmm = (df["time"].dt.hour * 100 + df["time"].dt.minute).to_numpy()
        # 盘中累计 VWAP（当日截至当前bar的量额比），作为移动网格中枢（真实价口径）
        cum_amt = df.groupby("date")["amount"].cumsum()
        cum_vol = df.groupby("date")["volume"].cumsum()
        vwap = (cum_amt / cum_vol.replace(0, np.nan)).to_numpy(dtype=np.float64)
        # 趋势防御的 MA60 用前复权收盘价，消除除权跳空；网格中枢/成交仍用真实价。
        daily_close = df.groupby("date")["signal_close"].last()
        df.attrs["adjustment"] = adjustment
        df.attrs["corporate_action_dates"] = corporate_dates
        trend_ma_n = int(p.get("trend_ma", 60))
        daily_ma = daily_close.rolling(trend_ma_n, min_periods=trend_ma_n).mean()
        daily_slope = daily_ma.diff(5)
        trend_defense = ((daily_close < daily_ma) & (daily_slope < 0)).shift(1).astype("boolean").fillna(False).to_dict()

        acc = bg.SimAccount(init_cash, p)
        trades = []
        rejected = []
        equity_curve = []

        # 建底仓（首日第一根 bar）；中枢 = 当日盘中VWAP（每 bar 更新）
        base_price = None
        grid_bought = 0
        prev_day = None
        anchor_price = float(closes[0])
        stopped = False

        for i in range(len(df)):
            ts = times[i]
            day = dates[i]
            h = hhmm[i]
            cur = closes[i]

            if day != prev_day:
                acc.new_day()
                prev_day = day

            if base_price is None:
                ok = acc.buy(cur, base_position) if base_position > 0 else True
                if base_position > 0 and not ok:
                    rejected.append({"time": ts, "direction": "BUY", "price": cur,
                                     "shares": base_position,
                                     "reason": f"建底仓现金不足(需{cur*base_position:,.0f}元)"})
                elif base_position > 0:
                    base_price = acc.last_fill_price
                    trades.append({"time": ts, "direction": "BUY", "price": acc.last_fill_price,
                                   "shares": base_position, "reason": "建底仓", "score": "",
                                   "fee": round(acc.last_fee, 2), "t_pnl": None})
                else:
                    base_price = cur
                continue

            raw_center = vwap[i] if not np.isnan(vwap[i]) else closes[i]
            base_price = max(raw_center, anchor_price * (1 - float(p.get("center_freeze_pct", 8.0)) / 100.0))

            account_loss = acc.equity(cur) / init_cash - 1
            hard_stop = cur <= base_price * (1 - float(p.get("hard_stop_pct", 12.0)) / 100.0)
            loss_stop = account_loss <= -float(p.get("max_grid_loss_pct", 5.0)) / 100.0
            if hard_stop or loss_stop:
                stopped = True
                sellable_t = max(0, min(acc.closeable, acc.total - base_position))
                if sellable_t > 0:
                    sold = acc.sell(cur, sellable_t)
                    trades.append({"time": ts, "direction": "SELL", "price": acc.last_fill_price,
                                   "shares": sold, "reason": "网格硬止损", "score": "",
                                   "fee": round(acc.last_fee, 2),
                                   "t_pnl": round(acc.last_t_pnl, 2) if acc.last_t_pnl is not None else None})
                    grid_bought = max(0, grid_bought - sold // max(trade_shares, 1))

            # 尾盘平 T 仓（可选）
            if enable_close and h >= 1455:
                t_shares = max(0, acc.total - base_position)
                if t_shares > 0:
                    acc.sell(cur, t_shares)
                    trades.append({"time": ts, "direction": "SELL", "price": acc.last_fill_price,
                                   "shares": t_shares, "reason": "尾盘平T仓", "score": "",
                                   "fee": round(acc.last_fee, 2),
                                   "t_pnl": round(acc.last_t_pnl, 2) if acc.last_t_pnl is not None else None})
                    grid_bought = 0
                continue

            # 卖出：涨回上一格（grid_bought>0 且价格回到对应档位）
            if grid_bought > 0:
                sell_line = base_price * (1 - grid_step * (grid_bought - 1))
                if cur >= sell_line:
                    sellable = max(0, acc.total - base_position)
                    if sellable >= trade_shares:
                        acc.sell(cur, trade_shares)
                        trades.append({"time": ts, "direction": "SELL",
                                       "price": acc.last_fill_price,
                                       "shares": trade_shares,
                                       "reason": f"网格卖出(回到{grid_bought-1}档)", "score": "",
                                       "fee": round(acc.last_fee, 2),
                                       "t_pnl": round(acc.last_t_pnl, 2) if acc.last_t_pnl is not None else None})
                        grid_bought -= 1
                    else:
                        rejected.append({"time": ts, "direction": "SELL", "price": cur,
                                         "shares": trade_shares,
                                         "reason": f"网格卖出但T仓不足(T+1/持仓{acc.closeable})"})

            # 买入：跌破下一格
            # 趋势防御：MA60 向下时暂停新买入。此处键为 date 类型，须用 date 查询
            # （历史上误用 pd.Timestamp(day) 导致 hash 不匹配、防御恒为 False，从未生效）。
            defense = bool(trend_defense.get(day, False))
            if grid_bought < grid_levels and not stopped and not defense:
                buy_line = base_price * (1 - grid_step * (grid_bought + 1))
                if cur <= buy_line:
                    if acc.total + trade_shares > max_position:
                        rejected.append({"time": ts, "direction": "BUY", "price": cur,
                                         "shares": trade_shares, "reason": "网格买入超最大持仓"})
                    elif acc.cash < cur * trade_shares:
                        rejected.append({"time": ts, "direction": "BUY", "price": cur,
                                         "shares": trade_shares, "reason": "网格买入现金不足"})
                    else:
                        ok = acc.buy(cur, trade_shares)
                        if ok:
                            trades.append({"time": ts, "direction": "BUY",
                                           "price": acc.last_fill_price,
                                           "shares": trade_shares,
                                           "reason": f"网格买入(-{grid_bought+1}档)", "score": "",
                                           "fee": round(acc.last_fee, 2), "t_pnl": None})
                            grid_bought += 1
                        else:
                            rejected.append({"time": ts, "direction": "BUY", "price": cur,
                                             "shares": trade_shares, "reason": "网格买入未成交"})

            equity_curve.append({"time": ts, "equity": acc.equity(cur),
                                 "close": cur, "cash": acc.cash, "position": acc.total})

        acc.rejected = rejected
        return self._build_result(acc, trades, pd.DataFrame(equity_curve), df, init_cash, p)

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
        dd = (eq2["equity"] - peak) / peak
        mdd = float(dd.min())
        calmar = annual / abs(mdd) if mdd < 0 else float("nan")

        sells = trades_df[trades_df["direction"] == "SELL"] if len(trades_df) else trades_df
        t_sells = sells[sells["t_pnl"].notna()] if len(sells) else sells
        t_wins = t_sells[t_sells["t_pnl"] > 0] if len(t_sells) else t_sells
        buys = trades_df[trades_df["direction"] == "BUY"] if len(trades_df) else trades_df
        base_price = None
        base_rows = trades_df[trades_df["reason"].astype(str).str.contains("建底仓")] if len(trades_df) else trades_df
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
            base_shares=int(params.get("base_position", 0)),
            avg_exposure=float(exposure.mean()),
            max_exposure=float(exposure.max()),
            capital_return=((final_equity - init_cash) / avg_capital
                            if avg_capital > 0 else 0.0),
            data_adjustment=str(df.attrs.get("adjustment", "不复权")),
        )
