# -*- coding: utf-8 -*-
"""三个可观察候选的执行层压力测试。

维度：漏单 1/5/10%、延迟 1/3/5 根bar、涨跌停近似。
所有维度叠加在信号→撮合之间，策略信号逻辑不变；每个维度可单独开关。
成交量约束按口径说明不做，仅保留 PaperBroker 原默认。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.adjustment import prepare_signal_prices  # noqa: E402
from core.grid_shared_live import GridSharedLiveEngine  # noqa: E402
from core.ma_swing_live import MaSwingLiveEngine  # noqa: E402
from core.paper_broker import PaperBroker, PaperBrokerConfig  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager  # noqa: E402
from core.sentiment_t_shared_live import SentimentTSharedLiveEngine  # noqa: E402
from core.stress_execution import StressExecutionEngine  # noqa: E402
from strategies import get_strategy  # noqa: E402

OUT_DIR = Path(r"D:\Codex输出\压力测试")
INIT_CASH = 1_000_000.0

CANDIDATES = [
    ("300308", "grid_trade"),
    ("300308", "ma_swing"),
    ("688256", "sentiment_t"),
]


def _params(code, sid):
    strategy = get_strategy(sid)
    params = strategy.default_params()
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    if sid == "ma_swing":
        params["position_pct"] = 30.0
    else:
        first_price = float(df["close"].iloc[0])
        base = int((INIT_CASH * 0.30 / first_price) // 100 * 100)
        if base < 100:
            raise ValueError("资金不足100股")
        params["base_position"] = base
        if "fixed_shares" in params:
            params["fixed_shares"] = max(100, int(base * 0.1 // 100 * 100))
        if "trade_shares" in params:
            params["trade_shares"] = max(100, int(base * 0.1 // 100 * 100))
        params["max_position"] = max(base * 2, base + 100)
    return df, params


def _engine(code, sid, params, df):
    symbol = f"{code}.SH" if code.startswith("68") else f"{code}.SZ"
    if sid == "grid_trade":
        return symbol, GridSharedLiveEngine(symbol, params, df=df)
    if sid == "ma_swing":
        prepared, events, _ = prepare_signal_prices(df, code)
        return symbol, MaSwingLiveEngine(symbol, params, prepared_df=prepared,
                                         corporate_action_dates=events)
    if sid == "sentiment_t":
        return symbol, SentimentTSharedLiveEngine(symbol, params, df=df)
    raise ValueError(sid)


def _broker(params, df):
    risk = RiskManager(RiskConfig(max_symbol_position_pct=1, max_account_position_pct=1,
                                  max_order_value_pct=1, max_daily_loss_pct=1,
                                  max_symbol_cumulative_loss_pct=1))
    return PaperBroker(INIT_CASH, df.time.iloc[0].date().isoformat(), risk,
                       PaperBrokerConfig(
                           commission_rate=params["commission_rate"],
                           min_commission=params["min_commission"],
                           stamp_tax_rate=params["stamp_tax_rate"],
                           transfer_fee_rate=params["transfer_fee_rate"],
                           slippage_bps=params.get("slippage_bps", 0),
                           max_volume_participation=1))


def _metrics(engine, init_cash):
    eq = pd.DataFrame(engine.equity_curve)
    if len(eq) == 0:
        return np.nan, np.nan, 0, 0, 0
    final = float(eq["equity"].iloc[-1])
    total_return = final / init_cash - 1
    peak = eq["equity"].cummax()
    dd = (eq["equity"] - peak) / peak
    mdd = float(dd.min())
    filled = sum(1 for s in engine.signal_log if (s.get("filled_quantity") or 0) > 0)
    dropped = sum(1 for s in engine.signal_log if s.get("order_status") == "DROPPED")
    rejected = sum(1 for s in engine.signal_log if s.get("order_status") in {"REJECTED", "UNKNOWN"})
    return total_return, mdd, filled, dropped, rejected


def run_one(code, sid, *, drop_rate=0.0, latency_bars=0, apply_limit=False, seed=42):
    df, params = _params(code, sid)
    symbol, live = _engine(code, sid, params, df)
    engine = StressExecutionEngine(_broker(params, df), drop_rate=drop_rate,
                                   latency_bars=latency_bars, apply_limit=apply_limit, seed=seed)
    engine.run(df, symbol, live.on_bar)
    total_return, mdd, filled, dropped, rejected = _metrics(engine, INIT_CASH)
    return {
        "股票": code, "策略": sid, "漏单%": drop_rate * 100,
        "延迟bar": latency_bars, "涨跌停近似": apply_limit,
        "总收益%": round(total_return * 100, 2),
        "最大回撤%": round(mdd * 100, 2),
        "成交笔数": filled, "漏单笔数": dropped, "拒单笔数": rejected,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    scenarios = []
    scenarios += [dict(drop_rate=p) for p in (0.0, 0.01, 0.05, 0.10)]
    scenarios += [dict(latency_bars=k) for k in (1, 3, 5)]
    scenarios += [dict(apply_limit=True)]
    rows = []
    for code, sid in CANDIDATES:
        for sc in scenarios:
            try:
                rows.append(run_one(code, sid, **sc))
            except Exception as exc:  # noqa: BLE001
                rows.append({"股票": code, "策略": sid, "漏单%": sc.get("drop_rate", 0) * 100,
                             "延迟bar": sc.get("latency_bars", 0),
                             "涨跌停近似": sc.get("apply_limit", False), "错误": str(exc)})
    detail = pd.DataFrame(rows)
    detail.to_csv(OUT_DIR / "候选执行层压力测试.csv", index=False, encoding="utf-8-sig")
    print(detail.to_string(index=False))


if __name__ == "__main__":
    main()