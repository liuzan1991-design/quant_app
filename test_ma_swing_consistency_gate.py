# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from reconcile_ma_swing import reconcile

OUT = APP_DIR / "test_outputs" / "ma_swing_reconcile"
THRESHOLD = 0.95


def classify(row):
    if row["_merge"] == "left_only":
        return "信号差异：仅快速回测产生"
    if row["_merge"] == "right_only":
        return "信号差异：仅严格增量产生"
    if abs(float(row.get("share_diff", 0) or 0)) > 100:
        return "仓位差异：订单数量超过100股"
    if abs(float(row.get("time_diff_minutes", 0) or 0)) > 0:
        return "执行差异：成交时间不同"
    price_diff = row.get("price_diff_pct", 0)
    if pd.notna(price_diff) and abs(float(price_diff)) > 0.002:
        return "执行差异：成交价格偏差超过20bps"
    return "一致"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    failed = []
    for code in ["300308", "300502", "688256"]:
        merged, fast, live, _ = reconcile(code)
        merged["自动归因"] = merged.apply(classify, axis=1)
        merged.to_csv(OUT / f"{code}_trade_reconcile_attributed.csv",
                      index=False, encoding="utf-8-sig")
        matched = int((merged["_merge"] == "both").sum())
        denominator = max(len(fast), len(live), 1)
        rate = matched / denominator
        rows.append({"股票": code, "快速交易数": len(fast), "增量交易数": len(live),
                     "同日同向匹配": matched, "匹配率": rate,
                     "门禁阈值": THRESHOLD, "是否通过": rate >= THRESHOLD})
        if rate < THRESHOLD:
            failed.append(f"{code}={rate:.2%}")
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT / "consistency_gate.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))
    if failed:
        raise AssertionError("一致性门禁未通过：" + ", ".join(failed))


if __name__ == "__main__":
    main()
