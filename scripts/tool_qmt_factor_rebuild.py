#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P1-a 复权口径重建工具（只读）：QMT `get_divid_factors` → 后复权因子 → 与 AD 全序列比对。

为什么需要它
------------
切源（AmazingData → QMT）时，**复权是最难的一块**：AD 直接给现成的"后复权因子" CSV，
QMT 只给**逐事件**的除权信息（派息/送转/配股 + `dr` 每事件比例）。本工具把 QMT 事件
重建为累计后复权因子，并与 AD 缓存逐日比对，回答：
  ① 事件日期是否对齐（差几天）
  ② 每事件幅度是否一致
  ③ 全序列最大偏差 + 偏差出现在哪个事件之后

红线
----
- **只读**：只调 `xtdata.get_divid_factors`，不 import `xttrader`、不碰账户/下单。
- **不写生产 `data/`**：导出物只落 `test_outputs/qmt_factor_rebuild/`。

用法
----
  D:\\Anaconda3\\python.exe scripts/tool_qmt_factor_rebuild.py            # 比对 300308
  D:\\Anaconda3\\python.exe scripts/tool_qmt_factor_rebuild.py --emit     # 另导出 QMT 口径因子 CSV
  D:\\Anaconda3\\python.exe scripts/tool_qmt_factor_rebuild.py --selftest # 公式自检（不联网）

产物
----
  test_outputs/qmt_factor_rebuild/factor_{code}.json   机读结论（事件表 + 偏差分布）
  test_outputs/qmt_factor_rebuild/events_{code}.csv    逐事件对照明细
  test_outputs/qmt_factor_rebuild/factor_qmt_{code}.csv（仅 --emit）QMT 口径累计因子
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = APP_DIR / "test_outputs" / "qmt_factor_rebuild"

# 判定阈值
EVENT_TOL = 1e-5          # 单事件幅度一致阈值（AD 因子为 6~7 位有效数字，1e-5 足够严）
SERIES_TOL = 1e-6         # 全序列一致阈值（相对）


def qmt_code(code: str) -> str:
    code = code.strip().upper()
    return code if "." in code else f"{code}.SZ"


def rebuild_factor(event_dates: list[pd.Timestamp], drs: list[float],
                   dates: pd.DatetimeIndex) -> pd.Series:
    """按事件重建累计后复权因子：f(t) = Π{dr : event_date > t}，最新日为 1.0。

    与 AD「后复权因子」同口径（历史价 × f(t) 得到复权价）。
    """
    out = []
    for d in dates:
        f = 1.0
        for ed, dr in zip(event_dates, drs):
            if ed > d:
                f *= dr
        out.append(f)
    return pd.Series(out, index=dates)


# ────────────────────────────── 自检 ──────────────────────────────
def selftest() -> int:
    checks = []
    dates = pd.DatetimeIndex(["2024-01-01", "2024-06-01", "2024-06-05", "2024-06-06", "2024-06-07"])
    ev = [pd.Timestamp("2024-06-06")]
    f = rebuild_factor(ev, [1.4], dates)
    checks.append(("事件前乘 1.4", f.loc["2024-06-05"] == 1.4, str(f.tolist())))
    checks.append(("事件当日不乘", f.loc["2024-06-06"] == 1.0, ""))
    checks.append(("事件后为 1.0", f.loc["2024-06-07"] == 1.0, ""))
    # 两事件累乘
    f2 = rebuild_factor([pd.Timestamp("2024-06-06"), pd.Timestamp("2024-06-10")],
                        [1.4, 1.0015], dates)
    checks.append(("两事件累乘", abs(f2.loc["2024-06-01"] - 1.4 * 1.0015) < 1e-12, str(f2.tolist())))
    # 理论除权比例公式：10 转 4 + 每股派 0.45，前收 166.96
    theory = 166.96 / ((166.96 - 0.45) / 1.4)
    checks.append(("理论比例 1.40378", abs(theory - 1.403784) < 1e-5, f"{theory:.6f}"))

    failed = [c for c in checks if not c[1]]
    for n, ok, d in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
    print(f"selftest: {len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="P1-a 复权口径重建与比对（只读）")
    ap.add_argument("--code", default="300308")
    ap.add_argument("--emit", action="store_true", help="导出 QMT 口径累计因子 CSV")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    ad_path = APP_DIR / "data" / f"factor_{args.code}.csv"
    if not ad_path.is_file():
        print(f"⛔ 缺 AD 因子缓存: {ad_path}")
        return 2
    try:
        from xtquant import xtdata
    except Exception as exc:
        print(f"⛔ xtquant 不可用: {exc}")
        return 2
    try:
        q = xtdata.get_divid_factors(qmt_code(args.code))
    except Exception as exc:
        print(f"⛔ QMT 取复权因子失败（miniQMT 行情端未就绪？）: {type(exc).__name__}: {exc}")
        return 2
    if q is None or len(q) == 0:
        print("⛔ QMT 返回空事件表")
        return 2

    ad = pd.read_csv(ad_path, encoding="utf-8-sig", parse_dates=["date"]
                     ).set_index("date")["factor"].astype(float).sort_index()
    q = q.copy()
    # QMT 的 time 是 epoch(ms)，按**交易所时区**换算（H25 口径：用 epoch，不硬编码时差）
    q["dt"] = (pd.to_datetime(q["time"], unit="ms").dt.tz_localize("UTC")
               .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize())
    q = q.sort_values("dt").reset_index(drop=True)

    # ① 逐事件：AD 因子在事件日的跳变 vs QMT dr（允许 ±3 天标签差，记录偏移）
    events = []
    for _, r in q.iterrows():
        d = r["dt"]
        pos = ad.index.searchsorted(d)
        best = None
        for off in (0, -1, 1, 2, 3, -2, -3):
            i = pos + off
            if 0 <= i < len(ad) and i >= 1:
                ratio = float(ad.iloc[i] / ad.iloc[i - 1])
                dev = abs(ratio - float(r["dr"]))
                if best is None or dev < best[2]:
                    best = (off, ad.index[i], dev, ratio)
        events.append({
            "qmt_date": str(d.date()), "dr": round(float(r["dr"]), 6),
            "interest": float(r["interest"]), "stock_bonus": float(r["stockBonus"]),
            "stock_gift": float(r["stockGift"]), "allot_num": float(r["allotNum"]),
            "ad_date": str(best[1].date()) if best else None,
            "ad_ratio": round(best[3], 6) if best else None,
            "dev": round(best[2], 6) if best else None,
            "day_offset": best[0] if best else None,
            "match": bool(best and best[2] <= EVENT_TOL),
        })

    # ② 全序列：AD 归一化（ad/ad_last，前复权乘数） vs QMT 累乘的倒数
    #    AD 因子 = Π{事件 ≤ t}（自上市累乘，升序）；用于 signal_close 的是 ad/ad_last
    #    = Π{事件 > t}^-1。QMT 重建的 f(t) = Π{事件 > t} ⇒ 取倒数后才同口径。
    ad_norm = ad / float(ad.iloc[-1])
    qmt_f = rebuild_factor([pd.Timestamp(e["qmt_date"]) for e in events],
                           [e["dr"] for e in events], pd.DatetimeIndex(ad.index))
    qmt_norm = 1.0 / qmt_f
    dev_series = (ad_norm - qmt_norm).abs()
    worst_date = dev_series.idxmax()

    # ③ 定位偏差台阶：哪一天之后偏差变大（= 哪个事件口径不一致）
    step = dev_series.diff().abs()
    steps = step[step > SERIES_TOL]
    culprits = []
    for d, v in steps.items():
        prev_events = [e for e in events if pd.Timestamp(e["qmt_date"]) < d]
        culprits.append({"date": str(d.date()), "step": round(float(v), 8),
                         "事件": prev_events[-1]["qmt_date"] if prev_events else None})

    result = {
        "code": args.code, "n_events": len(events),
        "events_all_match": all(e["match"] for e in events),
        "event_mismatches": [e for e in events if not e["match"]],
        "series": {"max_dev": round(float(dev_series.max()), 8),
                   "max_dev_date": str(worst_date.date()),
                   "n_dates": int(len(dev_series)),
                   "dates_over_tol": int((dev_series > SERIES_TOL).sum()),
                   "culprits": culprits},
        "ad_range": [str(ad.index[0].date()), str(ad.index[-1].date())],
        "qmt_range": [str(q["dt"].iloc[0].date()), str(q["dt"].iloc[-1].date())],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"factor_{args.code}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(events).to_csv(OUT_DIR / f"events_{args.code}.csv",
                                index=False, encoding="utf-8-sig")
    if args.emit:
        pd.DataFrame({"date": ad.index, "factor_ad_absolute": ad.values,
                      "ad_norm": ad_norm.values, "qmt_norm": qmt_norm.values,
                      "dev": dev_series.values}).to_csv(
            OUT_DIR / f"factor_qmt_{args.code}.csv", index=False, encoding="utf-8-sig")

    n_bad = len(result["event_mismatches"])
    print(f"✅ {args.code}: {len(events)} 个事件，{len(events) - n_bad}/{len(events)} 幅度一致"
          f"（阈值 {EVENT_TOL:g}）")
    for e in result["event_mismatches"]:
        print(f"  ⚠ {e['qmt_date']}: QMT dr={e['dr']} vs AD {e['ad_ratio']}"
              f"（偏差 {e['dev']}，{e['interest']}派/{e['stock_bonus']}送/{e['stock_gift']}转）")
    print(f"  全序列最大偏差 {result['series']['max_dev']:.8f}"
          f"（{result['series']['max_dev_date']}），超阈值日数 {result['series']['dates_over_tol']}"
          f"/{result['series']['n_dates']}")
    for c in culprits:
        print(f"  台阶 @ {c['date']}：Δ={c['step']}（源自事件 {c['事件']}）")
    print(f"  产物: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
