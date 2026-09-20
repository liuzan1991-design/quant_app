# -*- coding: utf-8 -*-
"""A21 复核 · 数据层：用补口前的备份做**逐行比对**（只读）。

用户指定的数据层两项：
  D1 **原有行是否保持不变** —— 备份里的每一行，必须在新文件中**原样存在**（逐行比 OHLCV）
  D2 **新增行是否只落在原本缺失区间** —— 新增的 time 应恰好是该标的原本缺的那些交易日 × 240

用法
----
    D:\\Anaconda3\\python.exe -X utf8 scripts\\tool_a21_datalayer_check.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import pandas as pd  # noqa: E402

BAK_DIR = Path(r"C:\Users\27329\quant_app_backups\data_gapfix_20260920_181651")
CUR_DIR = APP_DIR / "data"
STOCKS = ["000572", "002371", "300364", "300502", "601619"]
REF = "300308"
PRICE_COLS = ["open", "high", "low", "close", "volume", "amount"]
OUT_JSON = APP_DIR / "test_outputs" / "a21_datalayer_check.json"


def rd(p: Path) -> pd.DataFrame:
    d = pd.read_csv(p)
    d.columns = [c.strip().lstrip("\ufeff") for c in d.columns]
    d["time"] = pd.to_datetime(d["time"])
    return d


def main() -> int:
    print("A21 复核 · 数据层（只读；逐行比对备份 vs 现文件）")
    REF_JSON: dict = {}

    # 参考日历：300308 全区间交易日（用于判定"原本缺哪些交易日"）
    ref = rd(CUR_DIR / f"stock_{REF}_1m.csv")
    ref_days = set(ref["time"].dt.normalize().unique())

    REPAIR_START = pd.Timestamp("2024-01-01")
    REPAIR_END = pd.Timestamp("2025-08-31")

    print()
    print("=" * 100)
    print("D1 原有行是否保持不变（备份每一行 → 必须在新文件中原样存在）")
    print("=" * 100)
    print(f"{'代码':>8} {'备份行数':>9} {'现文件行数':>10} {'新增行':>9} "
          f"{'备份time全保留':>14} {'OHLCV逐行相同':>14}")
    print("-" * 100)

    rows = []
    for c in STOCKS:
        bak_p, cur_p = BAK_DIR / f"stock_{c}_1m.csv", CUR_DIR / f"stock_{c}_1m.csv"
        old, new = rd(bak_p), rd(cur_p)

        old_t, new_t = set(old["time"]), set(new["time"])
        kept = old_t.issubset(new_t)

        # 逐行比价格量列
        m = new.merge(old, on="time", suffixes=("_new", "_old"), how="inner")
        same = True
        mism = {}
        for col in PRICE_COLS:
            a, b = f"{col}_new", f"{col}_old"
            if a not in m.columns or b not in m.columns:
                continue
            ne = (m[a].round(6) != m[b].round(6))
            if ne.any():
                same = False
                mism[col] = int(ne.sum())

        added = len(new) - len(old)
        print(f"{c:>8} {len(old):>9} {len(new):>10} {added:>9} "
              f"{str(kept):>14} {str(same):>14}")
        if mism:
            print(f"          ⚠️ 数值不一致列: {mism}")
        rows.append({"code": c, "备份行数": len(old), "现文件行数": len(new),
                     "新增行": added, "备份time全保留": bool(kept),
                     "OHLCV逐行相同": bool(same), "不一致列": mism})

    print()
    n_ok = sum(1 for r in rows if r["备份time全保留"] and r["OHLCV逐行相同"])
    print(f"⇒ {n_ok}/{len(rows)} 只：备份中的**每一行**在新文件中均原样存在（time + OHLCV 逐行相同）")

    print()
    print("=" * 100)
    print("D2 新增行是否只落在原本缺失区间")
    print("=" * 100)
    print("判定：新增的 time（按日去重）应 = 参考日历在该标的原覆盖区间内、该标的原缺的交易日")
    print()
    print(f"{'代码':>8} {'新增交易日':>10} {'补入区间内':>11} {'区间外':>8} "
          f"{'区间外明细(前5)':<40}")
    print("-" * 100)
    rows2 = []
    for c in STOCKS:
        bak_p, cur_p = BAK_DIR / f"stock_{c}_1m.csv", CUR_DIR / f"stock_{c}_1m.csv"
        old, new = rd(bak_p), rd(cur_p)
        old_days = set(old["time"].dt.normalize().unique())
        new_days = set(new["time"].dt.normalize().unique())
        added_days = sorted(new_days - old_days)

        in_range = [d for d in added_days if REPAIR_START <= d <= REPAIR_END]
        out_range = [d for d in added_days if not (REPAIR_START <= d <= REPAIR_END)]

        # 原本缺失的交易日（参考日历 ∩ 原覆盖区间 ∩ 非原覆盖日）
        lo, hi = min(old_days), max(old_days)
        expect_missing = sorted({d for d in ref_days if lo <= d <= hi} - old_days)

        out_detail = ", ".join(str(d.date()) for d in out_range[:5])
        print(f"{c:>8} {len(added_days):>10} {len(in_range):>11} {len(out_range):>8} "
              f"{out_detail:<40}")
        rows2.append({"code": c, "新增交易日": len(added_days),
                      "补入区间内": len(in_range), "区间外": len(out_range),
                      "区间外明细": [str(d.date()) for d in out_range],
                      "原本缺失交易日数": len(expect_missing),
                      "新增日==原本缺失日": set(added_days) == set(expect_missing)})

    print()
    n2 = sum(1 for r in rows2 if r["区间外"] == 0)
    n3 = sum(1 for r in rows2 if r["新增日==原本缺失日"])
    print(f"⇒ {n2}/{len(rows2)} 只：新增行**全部**落在补入区间 2024-01-01 ~ 2025-08-31 内")
    print(f"⇒ {n3}/{len(rows2)} 只：新增的交易日**恰好等于**该标的原本缺失的交易日（无一多、无一少）")

    # 区间外的（若有）单独说明
    for r in rows2:
        if r["区间外"]:
            print(f"   ⚠️ {r['code']} 区间外新增 {r['区间外']} 个交易日：{r['区间外明细']}")

    REF_JSON["D1_原有行不变"] = rows
    REF_JSON["D2_新增行区间"] = rows2
    OUT_JSON.write_text(json.dumps(REF_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"报告已存：{OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
