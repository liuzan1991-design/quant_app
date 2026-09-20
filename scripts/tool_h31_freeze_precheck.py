# -*- coding: utf-8 -*-
"""H31 冻结模式前置检查（**全程只读**）—— 缓存覆盖 + 板块过滤的时间口径审计。

为什么不能直接调用 `load_industry_daily`
------------------------------------------
`core/sentiment_data.py:171` 结尾有 `merged.to_csv(target)` ——
**调用它本身就会写盘**，这正是 H31 的机理（缓存被刷新 ⇒ 结果漂移）。
⇒ 本脚本只读 CSV 并**复现判据分支**，不碰生产函数。

本脚本回答三个问题
------------------
1. 三份待重跑报告涉及的 (标的 × 窗口 × 行业) 组合里，
   有多少能命中缓存（冻结模式下不报错），多少会联网（冻结模式下报错）。
2. `load_industry_daily` 命中缓存时返回的是**未裁剪的全量**（`:147` 直接 `return existing`）
   ⇒ `compute_sector_status` 取 `iloc[-1]` 用的是**缓存最后一行**而非回测区间末行。
   实测这个差异是否**真的改变**了 sector 结论（即是否存在时间穿越）。
3. 覆盖判据只看首末 + `in_range>0`（H30 同款缺陷）⇒ 中段缺口能否被查出。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.sentiment_data import (  # noqa: E402
    STOCK_INDUSTRY, compute_sector_status, load_industry_level1,
)

DATA = APP_DIR / "data"

# ── 三份待重跑报告涉及的标的（取自报告明细 CSV，非猜测）──────────────
LONG_STOCKS = ["688256", "300308", "300502", "300418", "300364",
               "002371", "601619", "000572", "600900", "002594"]
ROLL_STOCKS = ["300308", "300502", "688256"]

LONG_PERIODS = [
    ("2023", "2023-01-01", "2023-12-29"),
    ("2024", "2024-01-02", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026YTD", "2026-01-01", "2026-08-21"),
    ("全周期", "2023-01-01", "2026-08-21"),
]
ROLL_WINDOWS = [
    ("滚动12月_202302", "2023-02-01", "2024-01-31"),
    ("滚动12月_202308", "2023-08-01", "2024-07-31"),
    ("滚动12月_202402", "2024-02-01", "2025-01-31"),
    ("滚动12月_202408", "2024-08-01", "2025-07-31"),
    ("滚动12月_202502", "2025-02-01", "2026-01-31"),
    ("滚动12月_202508", "2025-08-01", "2026-07-31"),
    ("样本外2025", "2025-01-01", "2025-12-31"),
    ("样本外2026YTD", "2026-01-01", "2026-08-21"),
]


def simulate_branch(ind_code: str, start: str, end: str):
    """复现 `sentiment_data.py:131-172` 的分支判定，**不写文件**。

    返回 (分支名, 命中时返回的 DataFrame 或 None)。
    """
    target = DATA / f"industry_{ind_code.replace('.', '_')}_daily.csv"
    if not target.exists():
        return "无缓存 → 联网", None
    existing = pd.read_csv(target, parse_dates=["time"])
    begin_dt, end_dt = pd.Timestamp(start), pd.Timestamp(end)
    if len(existing):
        first, last = existing["time"].min(), existing["time"].max()
        in_range = ((existing["time"] >= begin_dt) &
                    (existing["time"] <= end_dt.replace(hour=15, minute=0))).sum()
        if (first <= begin_dt + pd.Timedelta(days=5)
                and last >= end_dt.replace(hour=15, minute=0)
                and in_range > 0):
            return "命中缓存 → 返回**未裁剪全量**", existing
    return "未命中 → 联网重下", None


def main() -> None:
    l1 = load_industry_level1()
    cases = ([(c, *p) for c in LONG_STOCKS for p in LONG_PERIODS] +
             [(c, *w) for c in ROLL_STOCKS for w in ROLL_WINDOWS])

    print("=" * 100)
    print("【1】冻结模式可跑性：命中缓存 = 不报错；未命中 = 冻结模式会报错")
    print("=" * 100)
    stat: dict[str, int] = {}
    rows = []
    for code, label, start, end in cases:
        ind = l1.get(STOCK_INDUSTRY.get(code, ""))
        if not ind:
            rows.append((code, label, "-", "无行业映射 → 不触发板块分支", "", ""))
            stat["无行业映射"] = stat.get("无行业映射", 0) + 1
            continue
        branch, df = simulate_branch(ind, start, end)
        stat[branch] = stat.get(branch, 0) + 1
        rows.append((code, label, ind, branch, start, end))
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print(f"  {v:>3} 例  {k}")

    print()
    print("=" * 100)
    print("【2】时间穿越实测：命中缓存时 sector 用的是「缓存末行」还是「区间末行」？")
    print("=" * 100)
    print(f"{'标的':<8}{'窗口':<18}{'行业':<12}{'缓存末行':<12}{'区间末行':<12}"
          f"{'实际sector':<11}{'应得sector':<11}{'穿越?':<7}")
    drift = []
    for code, label, start, end in cases:
        ind = l1.get(STOCK_INDUSTRY.get(code, ""))
        if not ind:
            continue
        branch, df = simulate_branch(ind, start, end)
        if df is None:
            continue
        used = compute_sector_status(df)                       # 生产实际口径
        sliced = df[df["time"] <= pd.Timestamp(end)]           # 应得口径
        correct = compute_sector_status(sliced) if len(sliced) else "N/A"
        used_last = df["time"].iloc[-1].date()
        corr_last = sliced["time"].iloc[-1].date() if len(sliced) else "-"
        flag = "⚠️ 是" if used != correct else "否"
        if used != correct:
            drift.append((code, label, ind, used, correct, str(used_last)))
        print(f"{code:<8}{label:<18}{ind:<12}{str(used_last):<12}{str(corr_last):<12}"
              f"{used:<11}{correct:<11}{flag:<7}")

    print()
    print("=" * 100)
    print(f"【2 结论】时间穿越**实际改变结论**的组合：{len(drift)} 例")
    print("=" * 100)
    if drift:
        print(f"{'标的':<8}{'窗口':<18}{'行业':<12}{'实际':<10}{'应得':<10}{'缓存末行'}")
        for r in drift:
            print(f"{r[0]:<8}{r[1]:<18}{r[2]:<12}{r[3]:<10}{r[4]:<10}{r[5]}")
    else:
        print("  未发现差异（注意：这只说明当前缓存下恰好一致，不证明口径正确）")

    print()
    print("=" * 100)
    print("【3】覆盖判据的中段缺口盲区（H30 同款）：逐日检查区间内缺口")
    print("=" * 100)
    seen = set()
    for code, label, start, end in cases:
        ind = l1.get(STOCK_INDUSTRY.get(code, ""))
        if not ind or ind in seen:
            continue
        seen.add(ind)
        f = DATA / f"industry_{ind.replace('.', '_')}_daily.csv"
        if not f.exists():
            print(f"  {ind:<12} 无缓存文件")
            continue
        d = pd.read_csv(f, parse_dates=["time"]).sort_values("time")
        b, e = pd.Timestamp(start), pd.Timestamp(end)
        # 用指数分钟线的交易日作为"应有交易日"参照
        idx = pd.read_csv(DATA / "index_399006_SZ.csv", parse_dates=["time"])
        idx_days = set(pd.to_datetime(idx["time"]).dt.normalize().unique())
        want = {t for t in idx_days if b <= t <= e}
        have = set(d["time"].dt.normalize())
        miss = sorted(want - have)
        print(f"  {ind:<12} 区间 {b.date()}~{e.date()}: 应有 {len(want)} 个交易日, "
              f"缓存缺 {len(miss)} 个" + (f"  ← 首个缺口 {miss[0].date()}" if miss else ""))


if __name__ == "__main__":
    main()
