# -*- coding: utf-8 -*-
"""A20 补口工具：把 H29 那 5 只标的的缺失交易日补回来。

⚠️ 为什么不能用 `pool_fetch.py` / `incremental_update`
------------------------------------------------------
`core/data_fetch.py:126-140` 的覆盖判据是
「`first <= begin+5d` **且** `last >= end 15:00` **且** 区间内**有任意数据**（`in_range > 0`）」，
**不是真实交易日覆盖率**。实测这 5 只标的**全部命中早退分支**：

    代码      first        last        in_range   判定
    000572   2023-01-03   2026-08-21     9600  ★ 直接返回『无需更新』
    002371   2023-01-03   2026-08-14     2640  ★ 直接返回『无需更新』
    300364   2023-01-03   2026-08-21     9120  ★ 直接返回『无需更新』
    300502   2023-01-03   2026-08-21     2880  ★ 直接返回『无需更新』
    601619   2023-01-03   2026-09-18     9863  ★ 直接返回『无需更新』

⇒ 用原链路补数会**报成功但什么都不做**（假绿）。故本工具：
  ① **直接调 `fetch_kline`**（只返回 DataFrame、不写盘）；
  ② **纯追加合并**：只补本地没有的时间戳，**不修改任何已有一行**（避免 H8 那类重复写入）；
  ③ **写前备份、写后按覆盖率验证**。

用法
----
    # 干跑（默认，不写任何文件）：打印每只将新增多少根
    D:\\Anaconda3\\python.exe -X utf8 scripts/tool_data_gap_fill.py --dry-run

    # 实际补口（写盘 + 备份 + 验证）
    D:\\Anaconda3\\python.exe -X utf8 scripts/tool_data_gap_fill.py --apply

安全边界
--------
- **会占用 AmazingData 单点登录** ⇒ 内建采集窗口检查（本机 04:30–10:15 周一~五拒绝执行）。
- `--apply` 前会先把原文件整体复制到 `C:\\Users\\27329\\quant_app_backups\\data_gapfix_<时间戳>\\`。
- 回滚：把备份目录里的文件拷回 `quant_app/data/` 即可。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

DATA_DIR = APP_DIR / "data"
BACKUP_ROOT = Path(r"C:\Users\27329\quant_app_backups")
COLLECT_WINDOW = (time(4, 30), time(10, 15))     # 本机时间，与 QuantLiveRun 同源

DEFAULT_CODES = ["000572", "002371", "300364", "300502", "601619"]
REFERENCE = "300308"                              # 交易日参考日历（已知完整）


def in_collect_window() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.timetz().replace(tzinfo=None)
    return COLLECT_WINDOW[0] <= t <= COLLECT_WINDOW[1]


def read_csv(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p, parse_dates=["time"])
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    df["time"] = pd.to_datetime(df["time"])
    return df


def coverage(days: set, ref: set) -> float:
    lo, hi = min(days), max(days)
    exp = {d for d in ref if lo <= d <= hi}
    return len(days & exp) / len(exp) * 100 if exp else 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A20 补口（H29 缺口修复）")
    ap.add_argument("--codes", nargs="*", default=DEFAULT_CODES)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=None, help="默认取参考日历的最后一天")
    ap.add_argument("--dry-run", action="store_true", default=False)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    do_apply = args.apply and not args.dry_run
    print(f"模式：{'【APPLY 写盘】' if do_apply else '【DRY-RUN 不写盘】'}")

    now = datetime.now()
    print(f"本机时间：{now:%Y-%m-%d %H:%M:%S %a}")
    if in_collect_window():
        print("!! 落在采集窗口（本机 04:30–10:15 周一~五）⇒ 拒绝执行（会踢掉当天采集）")
        return 2
    print("采集窗口检查：通过")

    ref = read_csv(DATA_DIR / f"stock_{REFERENCE}_1m.csv")
    ref_days = set(ref["time"].dt.normalize().unique())
    end = args.end or max(ref_days).strftime("%Y-%m-%d")
    print(f"目标区间：{args.start} ~ {end}   参考日历：{REFERENCE}（{len(ref_days)} 个交易日）")
    print("=" * 104)

    from core.data_fetch import fetch_kline

    backup_dir = None
    if do_apply:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = BACKUP_ROOT / f"data_gapfix_{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        print(f"备份目录：{backup_dir}")
        print("=" * 104)

    report = []
    for code in args.codes:
        p = DATA_DIR / f"stock_{code}_1m.csv"
        if not p.exists():
            print(f"{code}  ❌ 本地文件不存在，跳过")
            continue
        old = read_csv(p)
        old_ts = set(old["time"])
        d_old = set(old["time"].dt.normalize().unique())
        cov_before = coverage(d_old, ref_days)

        if backup_dir is not None:
            shutil.copy2(p, backup_dir / p.name)

        try:
            new = fetch_kline(code, args.start, end, period="min1")
        except Exception as exc:  # noqa: BLE001
            print(f"{code}  ❌ 拉取异常：{type(exc).__name__}: {exc}")
            report.append(dict(code=code, ok=False, err=str(exc)))
            continue

        if new is None or not len(new):
            print(f"{code}  ❌ 源侧返回空")
            report.append(dict(code=code, ok=False, err="empty"))
            continue

        new["time"] = pd.to_datetime(new["time"])
        new["time"] = new["time"].astype(old["time"].dtype)
        add = new[~new["time"].isin(old_ts)].copy()
        n_add = len(add)

        # 实际写入发生在 2024-01-01 之后（2013 段本地已有），故只在区间内统计
        if do_apply and n_add:
            merged = (pd.concat([old, add], ignore_index=True)
                        .sort_values("time")
                        .reset_index(drop=True))
            merged = merged[list(old.columns)]
            merged.to_csv(p, index=False, encoding="utf-8-sig")

        # 验证（无论干跑都算，用于报告）
        if do_apply and n_add:
            chk = read_csv(p)
            chk_ts = set(chk["time"])
            d_new = set(chk["time"].dt.normalize().unique())
            cov_after = coverage(d_new, ref_days)
            # 原有行是否被改动：按 time 取交集比较全部数值列
            inter = chk[chk["time"].isin(old_ts)].sort_values("time").reset_index(drop=True)
            old_s = old.sort_values("time").reset_index(drop=True)
            same = inter.equals(old_s)
            mono = chk["time"].is_monotonic_increasing
            dup = int(chk["time"].duplicated().sum())
            print(f"{code}  ✅ 补 {n_add:>6} 根 ｜ 覆盖率 {cov_before:5.1f}% → {cov_after:5.1f}% ｜ "
                  f"原有行未改动：{same} ｜ 时间单调：{mono} ｜ 重复时间戳：{dup} ｜ 行数 {len(old)}→{len(chk)}")
            report.append(dict(code=code, ok=True, added=n_add, cov_before=round(cov_before, 1),
                               cov_after=round(cov_after, 1), preserved=same, monotonic=mono,
                               dup=dup, rows_before=len(old), rows_after=len(chk)))
        else:
            d_after = d_old | set(add["time"].dt.normalize().unique())
            cov_after = coverage(d_after, ref_days)
            print(f"{code}  将补 {n_add:>6} 根 ｜ 覆盖率 {cov_before:5.1f}% → {cov_after:5.1f}%（预计）")
            report.append(dict(code=code, ok=True, added=n_add, cov_before=round(cov_before, 1),
                               cov_after_expected=round(cov_after, 1)))

    print("=" * 104)
    if not do_apply:
        print("DRY-RUN 结束：**未写入任何文件**。确认后加 `--apply` 执行。")
    else:
        out = BACKUP_ROOT / f"data_gapfill_report_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"APPLY 结束。回滚：把 {backup_dir} 内的文件拷回 {DATA_DIR} 即可。")
        print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
