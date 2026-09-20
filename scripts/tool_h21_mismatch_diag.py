# -*- coding: utf-8 -*-
"""H21 诊断：sentiment_t 的 fast 路径 vs shared 状态机 信号失配归因（只读）。

用法
----
D:\\Anaconda3\\python.exe -X utf8 scripts/tool_h21_mismatch_diag.py [code ...]

不给 code 时跑全部 4 个标的（300308 / 300502 / 688256 / 600900）。

用途
----
`test_sentiment_t_shared_consistency.py` 的门禁是 `matched / max(len(fast), len(shared)) >= 0.95`。
本工具输出**失配明细 / 按日聚合 / 按方向聚合**，用来判断失配是"全局性分歧"还是"集中在边界日期"。

安全
----
显式装离线守卫（与 pytest 同机制）⇒ **不联网、不登录 AmazingData**。
口径说明：守卫下 `sentiment_t` 会降级为"无情绪过滤"；实测该口径与无守卫口径的
**失配结构一致**（600900 均为 6 left_only + 3 right_only）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import tests_offline_guard as guard  # noqa: E402

guard.install("tool_h21_mismatch_diag")

from core.sentiment_t_shared_live import SentimentTSharedLiveEngine  # noqa: E402
from strategies.sentiment_t import SentimentTStrategy  # noqa: E402

DEFAULT_CODES = ("300308", "300502", "688256", "600900")


def diagnose(code: str, verbose: bool = True) -> dict:
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
        for s in list(engine.on_bar(row, None)):
            trades.append({"time": ts, "direction": s.side.value, "shares": s.quantity})

    fast = strategy.run(df, 1_000_000, strategy.default_params(), context={"code": code})
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
    denom = max(len(fast_df), len(shared_df), 1)
    bad = merged[merged["_merge"] != "both"].copy()

    print(f"\n{'='*66}\n[{code}] rows={len(df)} last={df['time'].max()}")
    print(f"  fast={len(fast_df)} shared={len(shared_df)} matched={matched} "
          f"denom={denom} rate={matched/denom:.6f} "
          f"{'PASS' if matched/denom >= 0.95 else 'FAIL(<0.95)'}")

    if not len(bad):
        print("  无失配")
        return {"code": code, "fast": len(fast_df), "shared": len(shared_df),
                "matched": matched, "rate": matched / denom, "diff_days": 0}

    bad["day"] = pd.to_datetime(bad["time"]).dt.date
    if verbose:
        print(f"\n  --- 失配明细（{len(bad)} 行）---")
        print(bad.to_string(index=False))

    f_days = fast_df.assign(day=pd.to_datetime(fast_df["time"]).dt.date).groupby("day").size()
    s_days = shared_df.assign(day=pd.to_datetime(shared_df["time"]).dt.date).groupby("day").size()
    cmp_days = pd.concat([f_days.rename("fast"), s_days.rename("shared")],
                         axis=1).fillna(0).astype(int)
    diff_days = cmp_days[cmp_days["fast"] != cmp_days["shared"]]
    print(f"\n  --- 有差异的交易日（{len(diff_days)} / {len(cmp_days)} 天）---")
    print(diff_days.to_string())

    return {"code": code, "fast": len(fast_df), "shared": len(shared_df),
            "matched": matched, "rate": matched / denom, "diff_days": len(diff_days),
            "total_days": len(cmp_days), "bad_rows": len(bad)}


def main():
    codes = sys.argv[1:] or list(DEFAULT_CODES)
    rows = [diagnose(c) for c in codes]
    print(f"\n{'='*66}\n汇总")
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
