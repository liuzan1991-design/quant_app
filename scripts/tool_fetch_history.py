#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按年分块拉取某标的的 1 分钟历史，落盘为 `data/stock_<code>_1m.csv`。

为什么需要它
------------
`core.data_fetch.incremental_update` 是**单次** `fetch_kline(start, end)`，不分块。
跨几年一次拉（≈20 万行）容易让 AD 单轮跑很久，且失败后整段重来。
本工具按**自然年**切块，每块独立成败 —— 失败只重试当年，不影响已落盘的年份。

复用而非重写
------------
每块的落盘/去重/排序全部走 `incremental_update` 自己（它已处理"已覆盖则不更新"），
本工具只做**编排 + 验数 + 报告**。

安全边界
--------
AD 是**单点登录**：盘中拉数会踢掉 live 采集实例。因此默认拒绝在采集窗口内执行
（本机 04:30–10:10 = 北京 09:30–15:10），`--force` 可覆盖但不推荐。

用法
----
    python scripts/tool_fetch_history.py --code 300502 --start 2023-01-01
    python scripts/tool_fetch_history.py --code 300502 --force      # 盘中强拉（会踢采集）
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

import pandas as pd

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

# 采集窗口（本机时区）：04:30–10:10
WIN_START = dtime(4, 30)
WIN_END = dtime(10, 10)
BARS_PER_DAY = 240


def in_collect_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return now.weekday() < 5 and WIN_START <= now.time() <= WIN_END


def year_chunks(start: str, end: str) -> list[tuple[str, str]]:
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    out = []
    for y in range(s.year, e.year + 1):
        lo = max(s, pd.Timestamp(f"{y}-01-01"))
        hi = min(e, pd.Timestamp(f"{y}-12-31"))
        if lo <= hi:
            out.append((lo.strftime("%Y-%m-%d"), hi.strftime("%Y-%m-%d")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--dir", default="", help="落盘目录，默认 data/")
    ap.add_argument("--force", action="store_true", help="盘中强拉（会踢掉 live 采集）")
    args = ap.parse_args()

    end = args.end or datetime.now().strftime("%Y-%m-%d")
    data_dir = Path(args.dir) if args.dir else APP / "data"

    if in_collect_window() and not args.force:
        print("⛔ 当前在采集窗口内（本机 04:30–10:10）。AD 单点登录，拉数会踢掉 live 采集。")
        print("   收盘后再跑，或加 --force 强制（不推荐）。")
        return 2

    from core import data_fetch  # 延迟导入

    target = data_dir / f"stock_{args.code}_1m.csv"
    print(f"目标: {target}")
    print(f"区间: {args.start} ~ {end}（按年分块）\n")

    t_all = time.time()
    for lo, hi in year_chunks(args.start, end):
        t0 = time.time()
        for attempt in range(3):
            try:
                added, total, msg = data_fetch.incremental_update(args.code, lo, hi, data_dir)
                print(f"  {lo}~{hi}: {msg}  (+{added})  {time.time()-t0:.1f}s")
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    print(f"  {lo}~{hi}: 失败 {type(exc).__name__}: {str(exc)[:80]}")
                else:
                    time.sleep(3)

    print(f"\n总耗时 {time.time()-t_all:.1f}s")
    if not target.exists():
        print("✗ 目标文件不存在")
        return 1

    df = pd.read_csv(target, parse_dates=["time"])
    print(f"✓ {len(df):,} 行  {df['time'].min()} ~ {df['time'].max()}")
    per_day = df.groupby(df["time"].dt.date).size()
    bad = per_day[per_day != BARS_PER_DAY]
    print(f"  交易日 {len(per_day)} 天；非 {BARS_PER_DAY} 根的天: {len(bad)}")
    if len(bad):
        print(f"    样例: {bad.head(5).to_dict()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
