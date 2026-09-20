# -*- coding: utf-8 -*-
"""E5「验证机会」审计：把台账 §2.2 里人工维护的 `trend_ok` 计数变成可复算的。

背景（2026-09-20 登记）
----------------------
台账 §四 E5 的判据是「观察期内 `trend_ok` = 0 的天数占比 ≥ 80%」，
但排查发现：**`trend_ok` 从未落盘**——
- `live_outputs/300308.SZ/ma_swing/` 下只有 daily_history / daily_summary / **state**，无逐日信号序列；
- live 日志的 `[match]` 行只含 `新bar / 累计订单 / 累计成交 / 账户权益`，**不含 MA 或 trend_ok**。
⇒ 该计数**一直靠人工数在台账 §2.2 的备注里**，拿不出可复算证据。
本工具补上这条链，为 E5 提供**独立可复算**的读数。

⚠️ 设计原则：**不重写公式**，直接 import live 路径自己用的
`core.ma_swing_signals.shifted_execution_signals` 与 `strategies.ma_swing` 的默认参数，
只把数据换成**live 采的同一个文件**（`data/live/stock_{code}_1m.csv`，见 `core/market_collector.py:46`）。
⇒ 避免"另写一套实现 ⇒ 复现不一致"的风险（那比人工计数更糟）。

用法（只读，不联网、不写任何生产文件）
--------------------------------------
    D:\\Anaconda3\\python.exe -X utf8 scripts/tool_e5_trend_audit.py
    D:\\Anaconda3\\python.exe -X utf8 scripts/tool_e5_trend_audit.py --code 300308 --start 2026-09-09

输出：逐日 `trend_ok`（**原始口径** 与 **live 实际读取的 shift(1) 口径**）、
占比、以及 §四 E5 判据是否触发。JSON 落 `test_outputs/e5_trend_audit/`（gitignore）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

# 离线守卫：本工具只需读本地 CSV，装守卫是"防呆"（万一将来被人改成联网取数）
try:
    import tests_offline_guard as guard  # noqa: E402
    guard.install("tool_e5_trend_audit")
except Exception:
    pass

from core.ma_swing_signals import shifted_execution_signals, build_daily_signal_table  # noqa: E402
from strategies.ma_swing import MaSwingStrategy  # noqa: E402

OUT_DIR = APP_DIR / "test_outputs" / "e5_trend_audit"


def default_params() -> dict:
    """取策略自己的默认参数（不手抄，避免与代码漂移）。"""
    p = {}
    for meta in MaSwingStrategy.param_meta:
        p[meta.key] = meta.default
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="E5 trend_ok 审计（只读）")
    ap.add_argument("--code", default="300308")
    ap.add_argument("--start", default=None, help="观察期起始日 YYYY-MM-DD；默认取数据中 trend 可算的最早观察日")
    ap.add_argument("--csv", default=None, help="默认 data/live/stock_{code}_1m.csv（live 采集落盘位置）")
    args = ap.parse_args(argv)

    code = args.code.strip()
    csv_path = Path(args.csv) if args.csv else (APP_DIR / "data" / "live" / f"stock_{code}_1m.csv")
    if not csv_path.exists():
        print(f"!! 数据文件不存在：{csv_path}")
        print("   （注意 live 路径是 data/live/，静态池是 data/——两者不同，别混）")
        return 2

    df = pd.read_csv(csv_path, parse_dates=["time"])
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    df["time"] = pd.to_datetime(df["time"])
    print(f"数据：{csv_path}")
    print(f"      行数 {len(df)}  区间 {df['time'].min()} ~ {df['time'].max()}")

    params = default_params()

    # ── 口径一：原始（同日）── 
    raw = build_daily_signal_table(df, params)[["trend_ok", "trend_ma", "slow_ma",
                                                "fast_ma", "pullback_ma", "close"]]
    # ── 口径二：live 实际读取的 shift(1) ──
    shifted = shifted_execution_signals(df, params)[["trend_ok"]].rename(
        columns={"trend_ok": "trend_ok_live"})

    view = raw.join(shifted, how="inner")

    if args.start:
        view = view[view.index >= pd.Timestamp(args.start)]
    # 观察期口径：只保留 live 落盘覆盖到的交易日（与 §2.2 可比）
    start_ts = pd.Timestamp(args.start) if args.start else pd.Timestamp("2026-09-09")
    obs = view[view.index >= start_ts]

    print()
    print(f"=== 逐日 trend_ok（观察期自 {start_ts.date()} 起）===")
    hdr = f"{'日期':<12}{'trend_ok':>9}{'live(shift1)':>13}{'close':>10}{'MA20':>10}{'MA60':>10}{'MA10':>10}"
    print(hdr)
    print("-" * len(hdr))
    for ts, r in obs.iterrows():
        print(f"{ts.strftime('%Y-%m-%d'):<12}{str(bool(r['trend_ok'])):>9}"
              f"{str(bool(r['trend_ok_live'])):>13}"
              f"{r['close']:>10.2f}{r['trend_ma']:>10.2f}{r['slow_ma']:>10.2f}{r['pullback_ma']:>10.2f}")

    n = len(obs)
    if n == 0:
        print("\n!! 观察期内无可算日（数据不足）")
        return 3
    zero_raw = int((~obs["trend_ok"].astype(bool)).sum())
    zero_live = int((~obs["trend_ok_live"].astype(bool)).sum())
    print()
    print("=== E5 判据（trend_ok = 0 的天数占比 ≥ 80% ⇒ 触发）===")
    print(f"  口径一（原始同日）    : {zero_raw}/{n} = {zero_raw / n * 100:.1f}%"
          f"   {'✅ 触发' if zero_raw / n >= 0.8 else '❌ 未触发'}")
    print(f"  口径二（live shift1） : {zero_live}/{n} = {zero_live / n * 100:.1f}%"
          f"   {'✅ 触发' if zero_live / n >= 0.8 else '❌ 未触发'}")
    if zero_raw == zero_live:
        print("  ⇒ 两口径一致，结论不受 shift 影响。")
    else:
        print("  ⚠️ 两口径不一致 ⇒ 必须说明采用哪个口径（E5 应取 live 实际读取的那个）。")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = OUT_DIR / f"e5_trend_audit_{code}_{obs.index[0].date()}_{obs.index[-1].date()}.json"
    out_json.write_text(json.dumps({
        "code": code,
        "csv": str(csv_path),
        "params": params,
        "window": [str(obs.index[0].date()), str(obs.index[-1].date())],
        "days": n,
        "zero_trend_ok_raw": zero_raw,
        "zero_trend_ok_live_shift1": zero_live,
        "e5_trigger_raw": zero_raw / n >= 0.8,
        "e5_trigger_live": zero_live / n >= 0.8,
        "daily": [{"date": ts.strftime("%Y-%m-%d"),
                   "trend_ok": bool(r["trend_ok"]),
                   "trend_ok_live": bool(r["trend_ok_live"]),
                   "close": float(r["close"]),
                   "ma20": float(r["trend_ma"]),
                   "ma60": float(r["slow_ma"])} for ts, r in obs.iterrows()],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物：{out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
