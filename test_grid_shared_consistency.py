# -*- coding: utf-8 -*-
"""移动网格共享状态机 vs 快速路径黄金基准一致性验证。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import backtest_generic as bg  # noqa: E402
from core.adjustment import prepare_signal_prices  # noqa: E402
from core.grid_shared import GridSharedEngine  # noqa: E402
from strategies.grid_trade import GridTradeStrategy  # noqa: E402

GOLDEN_DIR = Path(r"D:\Codex输出\网格黄金基准")


def run_one(code: str) -> dict:
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    strategy = GridTradeStrategy()
    params = strategy.default_params()
    fast = strategy.run(df.copy(), 1_000_000.0, params, context={"code": code})

    prepared, _events, _adj = prepare_signal_prices(df, code)
    signal_close = {pd.Timestamp(r["time"]): float(r["signal_close"])
                    for _, r in prepared.iterrows()}

    engine_params = dict(params)
    engine_params["_init_cash"] = 1_000_000.0
    engine = GridSharedEngine(engine_params)
    account = bg.SimAccount(1_000_000.0, engine_params)
    trades = []
    for _, row in df.iterrows():
        ts = pd.Timestamp(row["time"])
        row = row.copy()
        row["signal_close"] = signal_close.get(ts, float(row["close"]))
        out = engine.on_bar(row, account)
        if out:
            for t in out:
                if not t.get("rejected"):
                    t.pop("rejected", None)
                    trades.append(t)

    fast_df = fast.trades[["time", "direction", "shares"]].copy()
    fast_df["time"] = pd.to_datetime(fast_df["time"])
    shared_df = pd.DataFrame(trades)
    if len(shared_df):
        shared_df["time"] = pd.to_datetime(shared_df["time"])
    else:
        shared_df = pd.DataFrame(columns=["time", "direction", "shares"])

    merged = fast_df.merge(shared_df, on=["time", "direction"], how="outer",
                           suffixes=("_fast", "_shared"), indicator=True)
    matched = int((merged["_merge"] == "both").sum())
    denominator = max(len(fast_df), len(shared_df), 1)
    rate = matched / denominator
    return {
        "code": code,
        "fast_count": len(fast_df),
        "shared_count": len(shared_df),
        "matched": matched,
        "match_rate": rate,
    }


def main() -> None:
    rows = [run_one(code) for code in ("300308", "300502", "688256")]
    result = pd.DataFrame(rows)
    print(result.to_string(index=False))
    out = APP_DIR / "test_outputs" / "grid_shared_consistency.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False, encoding="utf-8-sig")
    if any(r["match_rate"] < 0.95 for r in rows):
        raise SystemExit(2)


if __name__ == "__main__":
    main()