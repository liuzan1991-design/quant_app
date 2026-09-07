# -*- coding: utf-8 -*-
"""大盘情绪做T共享状态机 vs 新黄金基准一致性验证。"""
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
from core.sentiment_t_shared_live import SentimentTSharedLiveEngine  # noqa: E402
from strategies.sentiment_t import SentimentTStrategy  # noqa: E402

GOLDEN_DIR = Path(r"D:\Codex输出\情绪做T黄金基准")


def run_one(code: str) -> dict:
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    strategy = SentimentTStrategy()
    params = strategy.default_params()
    params["stop_loss_pct"] = {
        "low": params.pop("stop_loss_low"),
        "normal": params.pop("stop_loss_normal"),
        "high": params.pop("stop_loss_high"),
    }
    params.setdefault("min_bars", 10)
    params.setdefault("macd_fast", 5)
    params.setdefault("macd_slow", 10)
    params.setdefault("macd_signal", 3)
    params.setdefault("nine_turn_lookback", 3)
    params.setdefault("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
    params.setdefault("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
    params.setdefault("buy_back_gap_pct", 0.005)

    symbol = f"{code}.SH" if code.startswith("68") else f"{code}.SZ"
    engine = SentimentTSharedLiveEngine(symbol, params, df=df)

    trades = []
    for _, row in df.iterrows():
        ts = pd.Timestamp(row["time"])
        out = list(engine.on_bar(row, None))
        for s in out:
            trades.append({"time": ts, "direction": s.side.value, "shares": s.quantity})

    fast_params = strategy.default_params()
    fast = strategy.run(df, 1_000_000, fast_params, context={"code": code})
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
    return {"code": code, "fast_count": len(fast_df), "shared_count": len(shared_df),
            "matched": matched, "match_rate": rate}


def main() -> None:
    rows = [run_one(code) for code in ("300308", "300502", "688256", "600900")]
    result = pd.DataFrame(rows)
    print(result.to_string(index=False))
    out = APP_DIR / "test_outputs" / "sentiment_t_shared_consistency.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False, encoding="utf-8-sig")
    if any(r["match_rate"] < 0.95 for r in rows):
        raise SystemExit(2)


if __name__ == "__main__":
    main()