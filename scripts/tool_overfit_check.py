#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""H14 过拟合体检：Sharpe / bootstrap 区间 / Deflated Sharpe / 成本敏感性（只读）。

为什么需要
----------
项目此前**没有 Sharpe**，也没有任何数字回答「考虑到我试了这么多次，这个结果还显著吗」。
本工具把 `core/performance.py` 的统计量落到真实回测上，产出可复核的体检表。

用法
----
  D:\\Anaconda3\\python.exe scripts/tool_overfit_check.py                 # 两策略 × 4 窗口
  … --strategy grid_trade --no-cost-sens                                 # 快速版
  … --trials 30                                                          # 显式登记尝试次数

尝试次数（n_trials）为什么必须显式
--------------------------------
DSR 的门槛完全由「试了多少次」决定。项目至今的"选择"至少包括：4 策略 × 5 市场状态
× 多标的筛选 × 默认参数 —— 真实次数远大于 1。**不登记就默认 20 并在结果里标 approximation**，
绝不当成"试了 1 次"来算（那会把 DSR 抬高成假显著）。

产物
----
  test_outputs/overfit_check/report.json        机读全量结果
  test_outputs/overfit_check/summary.csv        每（策略×窗口）一行
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core import performance as pf  # noqa: E402
from core.data import load_data  # noqa: E402
from strategies import get_strategy  # noqa: E402

OUT = APP / "test_outputs" / "overfit_check"
COST_KEYS = ("commission_rate", "stamp_tax_rate", "transfer_fee_rate", "slippage_bps")


def windows(start: str = "2023-01-01", end: str = "2026-08-21") -> dict:
    last = pd.Timestamp(end)
    return {
        "全区间": (pd.Timestamp(start), last),
        "滚动12月_202406": (pd.Timestamp("2024-06-01"), pd.Timestamp("2025-05-31")),
        "样本外2025": (pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")),
        "样本外2026YTD": (pd.Timestamp("2026-01-01"), last),
    }


def run_one(sid: str, df: pd.DataFrame, params: dict) -> dict:
    strat = get_strategy(sid)
    r = strat.run(df.copy(), 1_000_000.0, params, {"code": "300308"})
    eq = pd.Series(r.equity["equity"].to_numpy(), index=pd.DatetimeIndex(r.equity["time"]))
    m = pf.summarize(eq)
    ret = pf.daily_returns(eq)
    sr = float(ret.mean() / ret.std(ddof=1)) if len(ret) > 1 and ret.std(ddof=1) > 0 else 0.0
    skew = float(ret.skew()) if len(ret) > 2 else 0.0
    kurt = float(ret.kurtosis() + 3.0) if len(ret) > 3 else 3.0
    # ⚠️ 单位必须与 `sharpe` 列一致：点估计是**年化** Sharpe，区间也必须年化，
    # 否则会出现"点 1.08、区间 [0.01, 0.14]"这种看起来矛盾的读数（首版就踩了）。
    scale = float(np.sqrt(pf.TRADING_DAYS_PER_YEAR))
    ci = pf.block_bootstrap_ci(
        ret.to_numpy(),
        lambda v: (float(np.mean(v) / np.std(v, ddof=1)) * scale) if np.std(v, ddof=1) > 0 else 0.0,
        n_boot=1000, block=5)
    return {"metrics": m, "sr_period": sr, "skew": skew, "kurt": kurt,
            "sharpe_ci": ci, "n_trades": len(r.trades), "fees": float(r.total_fees)}


def cost_sensitivity(sid: str, df: pd.DataFrame, params: dict, mult: float) -> float:
    p = dict(params)
    for k in COST_KEYS:
        if k in p and isinstance(p[k], (int, float)):
            p[k] = p[k] * mult
    r = get_strategy(sid).run(df.copy(), 1_000_000.0, p, {"code": "300308"})
    return float(r.total_return)


def main() -> int:
    ap = argparse.ArgumentParser(description="H14 过拟合体检（只读）")
    ap.add_argument("--strategy", default="both", choices=["grid_trade", "ma_swing", "both"])
    ap.add_argument("--trials", type=int, default=None, help="已登记的尝试次数（不填=20 并标 approximation）")
    ap.add_argument("--no-cost-sens", action="store_true", help="跳过成本敏感性（省时间）")
    args = ap.parse_args()

    sids = ["grid_trade", "ma_swing"] if args.strategy == "both" else [args.strategy]
    n_trials = args.trials if args.trials else 20
    trials_declared = args.trials is not None
    if not trials_declared:
        print("[WARN] 未用 --trials 登记尝试次数，按 20 计算 DSR（结果标 approximation）")

    full, _ = load_data("300308", "2023-01-01", "2026-08-21")
    full["time"] = pd.to_datetime(full["time"])
    rows, payload = [], {"generated_at": datetime.now().isoformat(timespec="seconds"),
                         "code": "300308", "n_trials": n_trials,
                         "trials_declared": trials_declared, "annualization": pf.TRADING_DAYS_PER_YEAR,
                         "items": []}

    for sid in sids:
        params = get_strategy(sid).default_params()
        for label, (b, e) in windows().items():
            df = full[(full["time"] >= b) & (full["time"] <= e)].reset_index(drop=True)
            got = run_one(sid, df, params)
            m = got["metrics"]
            dsr = pf.deflated_sharpe_ratio(got["sr_period"], m["days"], n_trials,
                                           got["skew"], got["kurt"])
            row = {"strategy": sid, "window": label, "days": m["days"],
                   "total_return": round(m["total_return"], 4),
                   "annual_return": round(m["annual_return"], 4),
                   "annual_vol": round(m["annual_vol"], 4),
                   "sharpe": round(m["sharpe"], 3),
                   "sharpe_lo": round(got["sharpe_ci"]["lo"], 3),
                   "sharpe_hi": round(got["sharpe_ci"]["hi"], 3),
                   "max_drawdown": round(m["max_drawdown"], 4),
                   "calmar": round(m["calmar"], 3),
                   "psr": round(dsr["psr"], 4), "sr0": round(dsr["sr0"], 5),
                   "dsr": round(dsr["dsr"], 4), "n_trades": got["n_trades"],
                   "dsr_approx": bool(dsr["approximation"])}
            if not args.no_cost_sens:
                base = m["total_return"]
                mults = {}
                for mult in (1.5, 2.0, 3.0):
                    r = cost_sensitivity(sid, df, params, mult)
                    mults[f"x{mult}"] = round(r, 4)
                row["cost_sens"] = mults
                row["cost_breakeven_x"] = next(
                    (mult for mult in (1.5, 2.0, 3.0) if mults[f"x{mult}"] <= 0), None)
            rows.append(row)
            payload["items"].append({"row": row, "sharpe_ci": got["sharpe_ci"],
                                     "sr_period": got["sr_period"]})
            print(f"{sid:10s} {label:16s} 收益 {row['total_return']:+.1%} "
                  f"Sharpe {row['sharpe']:.2f} [{row['sharpe_lo']:.2f},{row['sharpe_hi']:.2f}] "
                  f"MDD {row['max_drawdown']:.1%} DSR {row['dsr']:.3f}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT / "report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物: {OUT}")
    print("说明：Sharpe 年化系数 = 243（A 股）；DSR 用**单期** Sharpe；bootstrap 为块自举(block=5)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
