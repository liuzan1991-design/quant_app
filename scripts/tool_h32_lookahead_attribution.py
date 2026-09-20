# -*- coding: utf-8 -*-
"""H32 时间穿越的精确归因（**全程只读**）。

H31 冻结前置检查暴露了一个**更严重、且性质完全不同**的问题：
`load_industry_daily` 命中缓存时返回**未裁剪全量**（`sentiment_data.py:147` 直接 `return existing`），
而 `compute_sector_status` 取 `iloc[-1]` ⇒ **用缓存末行（2026-09）判断 2023 年的板块强弱**。

但"34 例 sector 字符串变了"**不等于 34 例结论都变了** —— 因为 H24 已实证：
**引擎只读 `crash` 与 `strong`，`weak` 与 `flat` 在决策上完全等价**。

⇒ 本脚本按 **H24 等价类**（{crash} / {strong} / {flat, weak}）重新归因，
    区分"**字符串变了但决策等价**"与"**真正改变了引擎输入**"。

同时做对照组：指数分钟线走同一套缓存分支，**是否也有穿越**？
（预期：无 —— 因为 `compute_market_status` 是**逐 bar 时间对齐**，
  而 `compute_sector_status` 是**取末行标量**。这个对比本身就是根因解释。）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.sentiment_data import (  # noqa: E402
    STOCK_INDUSTRY, compute_market_status, compute_sector_status, load_industry_level1,
)

DATA = APP_DIR / "data"

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

# H24 实证：引擎只读 crash 与 strong；weak 与 flat 决策等价
EQUIV = {"crash": "crash", "strong": "strong", "flat": "neutral", "weak": "neutral"}


def main() -> None:
    l1 = load_industry_level1()
    cases = ([(c, *p) for c in LONG_STOCKS for p in LONG_PERIODS] +
             [(c, *w) for c in ROLL_STOCKS for w in ROLL_WINDOWS])

    rows, missing = [], []
    for code, label, start, end in cases:
        ind = l1.get(STOCK_INDUSTRY.get(code, ""))
        if not ind:
            continue
        f = DATA / f"industry_{ind.replace('.', '_')}_daily.csv"
        if not f.exists():
            missing.append((code, label, ind, "无缓存文件"))
            continue
        d = pd.read_csv(f, parse_dates=["time"]).sort_values("time").reset_index(drop=True)
        b, e = pd.Timestamp(start), pd.Timestamp(end)
        # 复现覆盖判据（sentiment_data.py:144-147）
        first, last = d["time"].min(), d["time"].max()
        in_range = ((d["time"] >= b) & (d["time"] <= e.replace(hour=15, minute=0))).sum()
        hit = (first <= b + pd.Timedelta(days=5) and last >= e.replace(hour=15, minute=0)
               and in_range > 0)
        if not hit:
            missing.append((code, label, ind, f"未命中(缓存 {first.date()}~{last.date()})"))
            continue
        used = compute_sector_status(d)                       # 生产口径：全量末行
        sliced = d[d["time"] <= e]
        correct = compute_sector_status(sliced) if len(sliced) else "N/A"
        rows.append({
            "code": code, "label": label, "ind": ind,
            "used": used, "correct": correct,
            "used_last": d["time"].iloc[-1].date(),
            "corr_last": sliced["time"].iloc[-1].date() if len(sliced) else None,
            "str_changed": used != correct,
            "decision_changed": EQUIV.get(used) != EQUIV.get(correct),
        })

    df = pd.DataFrame(rows)
    n = len(df)
    print("=" * 96)
    print(f"【A】板块路径（industry_daily）—— 命中缓存 {n} 例，未命中 {len(missing)} 例")
    print("=" * 96)
    print(f"  字符串变了（sector 值不同）      : {df['str_changed'].sum():>3} / {n}"
          f"  ({df['str_changed'].mean()*100:.1f}%)")
    print(f"  ★ 决策真变了（跨 H24 等价类）    : {df['decision_changed'].sum():>3} / {n}"
          f"  ({df['decision_changed'].mean()*100:.1f}%)")
    print()
    print("  差异类型分布：")
    df["dtype_"] = df.apply(
        lambda r: ("无差异" if not r["str_changed"]
                   else ("★真影响 " + f"{r['used']}→{r['correct']}"
                         if r["decision_changed"] else
                         f"假差异(等价) {r['used']}→{r['correct']}")), axis=1)
    for k, v in df["dtype_"].value_counts().items():
        print(f"    {v:>3} 例  {k}")

    real = df[df["decision_changed"]]
    print()
    print("=" * 96)
    print(f"【B】★ 真正改变引擎输入的组合（{len(real)} 例）")
    print("=" * 96)
    if len(real):
        print(f"{'标的':<8}{'窗口':<18}{'行业':<12}{'生产用':<9}{'应得':<9}{'缓存末行':<12}{'区间末行'}")
        for _, r in real.sort_values(["code", "label"]).iterrows():
            print(f"{r['code']:<8}{r['label']:<18}{r['ind']:<12}{r['used']:<9}"
                  f"{r['correct']:<9}{str(r['used_last']):<12}{r['corr_last']}")
    print()
    print("  按标的汇总：")
    for code, g in real.groupby("code"):
        print(f"    {code:<8} {len(g)}/{len(df[df['code']==code])} 例真受影响"
              f"  ({', '.join(sorted(g['label'].unique())[:4])}{' ...' if len(g)>4 else ''})")

    if missing:
        print()
        print("=" * 96)
        print(f"【C】冻结模式会报错的组合（{len(missing)} 例）—— 必须先补缓存")
        print("=" * 96)
        for m in missing:
            print(f"  {m[0]:<8}{m[1]:<18}{m[2]:<12}{m[3]}")

    # ── 对照组：指数分钟线路径 ────────────────────────────────
    print()
    print("=" * 96)
    print("【D】对照组：指数分钟线（index_min）走同一套缓存分支 —— 是否也穿越？")
    print("=" * 96)
    idx = pd.read_csv(DATA / "index_399006_SZ.csv", parse_dates=["time"])
    print(f"  缓存覆盖 {idx['time'].min()} ~ {idx['time'].max()}")
    for label, start, end in [("2023", "2023-01-01", "2023-12-29"),
                              ("2026YTD", "2026-01-01", "2026-08-21")]:
        e = pd.Timestamp(end)
        # 生产口径：命中缓存 ⇒ 返回全量 ⇒ 逐 bar 建 map（多出的 bar 只是多余的 key）
        sm_used, _ = compute_market_status(idx)
        sm_corr, _ = compute_market_status(idx[idx["time"] <= e])
        days_used = {pd.Timestamp(t).date() for t in sm_used}
        days_corr = {pd.Timestamp(t).date() for t in sm_corr}
        want = {t.date() for t in idx["time"] if pd.Timestamp(start) <= t <= e}
        # 引擎只按回测区间的 bar 时间查表 ⇒ 只要区间内每个 bar 都能查到，就不穿越
        inner_used = {d: v for d, v in
                      ((pd.Timestamp(t).date(), v) for t, v in sm_used.items()) if d in want}
        inner_corr = {d: v for d, v in
                      ((pd.Timestamp(t).date(), v) for t, v in sm_corr.items()) if d in want}
        same = inner_used == inner_corr
        print(f"  {label:<10} 区间内交易日 {len(want):>3} | 全量map覆盖 {len(days_used)} 天"
              f" | 区间内取值一致: {'✅ 是（无穿越）' if same else '❌ 否'}")
    print()
    print("  ⇒ 机理对比：`compute_market_status` 把数据**逐 bar 映射成 key=时间**，")
    print("     多出的未来 bar 只是多余的 key，引擎按回测 bar 时间查表 ⇒ **天然无穿越**；")
    print("     而 `compute_sector_status` 取 **`iloc[-1]` 单值标量** ⇒ 末行是未来就穿越。")


if __name__ == "__main__":
    main()
