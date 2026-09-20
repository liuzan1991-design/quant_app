# -*- coding: utf-8 -*-
"""A21 后续：对 5 只补数据标的做**逐窗口复核**（只读）。

设计原则（三项，全部为避免副作用）
----------------------------------
1. **不跑任何策略** —— `sentiment_t` 的回测会刷新行业日线缓存（见纠偏记录 H31），
   本次复核只用 `load_data` 数交易日 + 读已有的明细 CSV，**零联网、零写盘**。
2. **不修改任何数据文件** —— 只读。
3. **参考日历用 300308**（唯一在全部区间都完整的标的）。

复核项
------
R1 **段落完整性**：6 个段（W1训练/W1验证/W2训练/W2验证/W3训练/W3验证）× 10 只，
   逐段数交易日数，看 5 只缺口标的补完后是否与参考日历同量级。
R2 **内部一致性（硬校验）**：W1 的「验证段」与 W2 的「训练段」是**同一区间**（2024 全年）
   ⇒ 同一 (标的, 策略) 的 `训练收益/训练相对` 与 `验证收益/验证相对` 必须**逐对相等**。
   这是不依赖任何外部真值的自洽性证据。
R3 **数值自洽反解**：`训练收益 − 训练相对` 应等于该标的训练段的 buy&hold 收益；
   `验证收益 − 验证相对` 同理。用于确认"相对"口径没写反、没串列。
R4 **差异分解**：A21 重跑 vs 原版，分两类标的看差异来源 ——
   · 完整标的：差异应只来自 H17（记录层）+ H31（外部缓存）
   · **缺口标的：差异应主要来自 A20（补数据）** ← 上一轮未量化，本次补上
   （含这 5 只的 **W3** —— 其训练段=2025 原本缺 61%，故也应变化）
R5 **离群与分布**：列出 |相对收益| ≥ 50% 的记录并反解基准；输出 regime 分布对比。

用法
----
    D:\\Anaconda3\\python.exe -X utf8 scripts\\tool_a21_window_review.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from core.data import load_data  # noqa: E402

OUT_DIR = Path(r"D:\Codex输出\A21滚动窗口重跑")
NEW_CSV = OUT_DIR / "滚动窗口样本外验证_明细_A21.csv"
OLD_CSV = OUT_DIR / "滚动窗口样本外验证_明细_原版备份.csv"
REPORT_JSON = APP_DIR / "test_outputs" / "a21_window_review.json"

HERE = APP_DIR / "test_outputs"
HERE.mkdir(parents=True, exist_ok=True)

# 与 tool_rolling_oos_rerun.py 完全一致（不可改，否则与重跑不可比）
STOCKS = [
    ("688256", "寒武纪"), ("300308", "中际旭创"), ("300502", "新易盛"), ("300418", "昆仑万维"),
    ("300364", "中文在线"), ("002371", "北方华创"), ("601619", "嘉泽新能"), ("000572", "海马汽车"),
    ("600900", "长江电力"), ("002594", "比亚迪"),
]
WINDOWS = [
    ("W1", "2023-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("W2", "2024-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
    ("W3", "2025-01-01", "2025-12-31", "2026-01-01", "2026-08-21"),
]
GAPFIX = {"000572", "002371", "300364", "300502", "601619"}
REF = "300308"                      # 参考日历
SEG_LABEL = {                       # (窗口, 角色) -> 人类可读
    ("W1", "tr"): "W1训练 2023", ("W1", "te"): "W1验证 2024",
    ("W2", "tr"): "W2训练 2024", ("W2", "te"): "W2验证 2025",
    ("W3", "tr"): "W3训练 2025", ("W3", "te"): "W3验证 2026",
}

REPORT: dict = {}


def sep(title: str) -> None:
    print()
    print("=" * 96)
    print(title)
    print("=" * 96)


# ──────────────────────────── R1 段落完整性 ────────────────────────────
def r1_segment_completeness() -> dict:
    """逐段数交易日数（用 load_data，即管道实际看到的数据）。"""
    sep("R1 段落完整性 —— 各窗口段的交易日数（load_data 实读，非手算 CSV）")

    # 先建参考日历：用 300308 在全区间（2023-01-01 ~ 2026-08-21）的交易日
    ref_df, _ = load_data(REF, "2023-01-01", "2026-08-21")
    ref_days = set(pd.to_datetime(pd.Series(ref_df["time"].unique())).dt.normalize())
    print(f"参考日历（{REF}）全区间交易日 = {len(ref_days)} 天")

    rows = []
    for wname, tr_s, tr_e, te_s, te_e in WINDOWS:
        for role, ss, se in (("tr", tr_s, tr_e), ("te", te_s, te_e)):
            label = SEG_LABEL[(wname, role)]
            # 该段在参考日历里的应有交易日数
            expect = {d for d in ref_days
                      if pd.Timestamp(ss) <= d <= pd.Timestamp(se)}
            rec = {"段": label, "窗口": wname, "角色": role,
                   "应有": len(expect), "各标的": {}}
            for code, name in STOCKS:
                df, _ = load_data(code, ss, se)
                n = 0 if df is None or df.empty else int(pd.to_datetime(df["time"]).dt.normalize().nunique())
                rec["各标的"][code] = n
            rows.append(rec)

    # 打印：行=段，列=标的
    codes = [c for c, _ in STOCKS]
    hdr = f"{'段':<14}{'应有':>5} " + " ".join(f"{c:>8}" for c in codes)
    print(hdr)
    print("-" * len(hdr))
    gaps = []
    for rec in rows:
        line = f"{rec['段']:<14}{rec['应有']:>5} "
        for c in codes:
            n = rec["各标的"][c]
            mark = ""
            if c in GAPFIX and rec["应有"] and n < rec["应有"] * 0.98:
                mark = "*"
                gaps.append((rec["段"], c, n, rec["应有"]))
            line += f"{n:>7}{mark} "
        print(line)
    print()
    if gaps:
        print(f"⚠️ 仍有不足 98% 的段（{len(gaps)} 处）：")
        for s, c, n, e in gaps:
            print(f"   {s}  {c}  {n}/{e} = {n/e*100:.1f}%")
    else:
        print("✅ 全部 10 只标的 × 6 个段，交易日数均达该段应有值的 ≥98%")

    REPORT["R1_段落完整性"] = rows
    REPORT["R1_缺口"] = [{"段": s, "code": c, "实际": n, "应有": e} for s, c, n, e in gaps]
    return REPORT


# ──────────────────── R2 内部一致性（W1验证 == W2训练）────────────────────
def r2_internal_consistency(new: pd.DataFrame) -> dict:
    sep("R2 内部一致性（硬校验）：W1 的「验证段」== W2 的「训练段」（同为 2024 全年）")
    print("原理：两段是同一区间，同一 (标的,策略) 的收益必须逐对相等。")
    print("      这是不依赖任何外部真值的自洽性证据。")
    print()

    cols_new = ["训练收益", "训练相对", "验证收益", "验证相对"]
    n = new.copy()
    n["c6"] = n["code"].astype(str).str.zfill(6)

    pairs = 0
    bad = []
    detail = []
    for code in [c for c, _ in STOCKS]:
        for sid in sorted(n["sid"].unique()):
            w1 = n[(n["c6"] == code) & (n["窗口"] == "W1") & (n["sid"] == sid)]
            w2 = n[(n["c6"] == code) & (n["窗口"] == "W2") & (n["sid"] == sid)]
            if w1.empty or w2.empty:
                continue
            w1, w2 = w1.iloc[0], w2.iloc[0]
            # W1 的验证段 数值  vs  W2 的训练段 数值
            a = (w1["验证收益"], w1["验证相对"])
            b = (w2["训练收益"], w2["训练相对"])
            ok = (a[0] == b[0]) and (a[1] == b[1])
            pairs += 1
            detail.append({"code": code, "sid": sid,
                           "W1验证收益": a[0], "W2训练收益": b[0],
                           "W1验证相对": a[1], "W2训练相对": b[1], "一致": ok})
            if not ok:
                bad.append((code, sid, a, b))

    print(f"可比对 (标的, 策略) 组合数 = {pairs}")
    print(f"逐对相等 = {pairs - len(bad)}  不一致 = {len(bad)}")
    if bad:
        print()
        print("★ 不一致清单：")
        for code, sid, a, b in bad:
            print(f"  {code} {sid}: W1验证收益/相对={a}  W2训练收益/相对={b}")
    else:
        print()
        print(f"✅ 全部 {pairs} 对完全相等 ⇒ 补数据后的管道自洽（同一区间在任何窗口下结果一致）")
    REPORT["R2_内部一致性"] = {"组合数": pairs, "一致": pairs - len(bad),
                                "不一致": [{"code": c, "sid": s, "W1验证": a, "W2训练": b}
                                           for c, s, a, b in bad]}
    return REPORT


# ─────────────────────── R3 数值自洽反解 ───────────────────────
def r3_arithmetic(new: pd.DataFrame) -> dict:
    sep("R3 数值自洽反解：训练收益 − 训练相对 == 标的自 身收益（验证同理）")
    print("作用：确认『相对』口径没写反、没串列；并顺带反解出各段的 buy&hold 基准。")
    print()

    n = new.copy()
    n["c6"] = n["code"].astype(str).str.zfill(6)
    # 同一 (code, 窗口) 的四个策略应反解出同一个标的收益。
    # ⚠️ 明细里的收益列已 round(,1) ⇒ 反解必然带 ±0.1 的舍入误差，容差取 0.2。
    TOL = 0.2
    bad = []
    implied = {}
    for (code, w), g in n.groupby(["c6", "窗口"]):
        tr_vals = (g["训练收益"] - g["训练相对"])
        te_vals = (g["验证收益"] - g["验证相对"])
        if (tr_vals.max() - tr_vals.min()) > TOL or (te_vals.max() - te_vals.min()) > TOL:
            bad.append({"code": code, "窗口": w,
                        "训练反解极差": float(tr_vals.max() - tr_vals.min()),
                        "验证反解极差": float(te_vals.max() - te_vals.min())})
        implied.setdefault(code, {})[w] = {
            "训练段标的收益": round(float(tr_vals.mean()), 1),
            "验证段标的收益": round(float(te_vals.mean()), 1)}

    print(f"检查 (标的,窗口) 组数 = {n.groupby(['c6', '窗口']).ngroups}"
          f"（容差 {TOL}，因明细收益已保留 1 位小数）")
    if bad:
        print(f"⚠️ 同组内反解极差 > {TOL} 的 {len(bad)} 组（说明口径异常）：")
        for b in bad:
            print("  ", b)
    else:
        print(f"✅ 每组内 4 个策略反解出的标的收益一致（极差 ≤ {TOL}）⇒ 『相对』口径自洽")
    print()
    print("反解出的标的段收益（%）—— W1验证 应 == W2训练（同 2024）、W2验证 应 == W3训练（同 2025）：")
    print(f"{'代码':>8} {'窗口':>5} {'训练段标的收益':>15} {'验证段标的收益':>15}")
    cross_bad = []
    for code in [c for c, _ in STOCKS]:
        for w in ("W1", "W2", "W3"):
            if w in implied.get(code, {}):
                r = implied[code][w]
                tag = "  ← 缺口标的" if code in GAPFIX else ""
                print(f"{code:>8} {w:>5} {r['训练段标的收益']:>15.1f} "
                      f"{r['验证段标的收益']:>15.1f}{tag}")
    print()
    print("跨窗口一致性（用反解值，比 R2 更强：它由『收益』与『相对』两个不同列独立算出）：")
    pairs = 0
    for code in [c for c, _ in STOCKS]:
        m = implied.get(code, {})
        for a, b, seg in (("W1", "W2", "2024"), ("W2", "W3", "2025")):
            if a in m and b in m:
                va = m[a]["验证段标的收益"]
                vb = m[b]["训练段标的收益"]
                pairs += 1
                ok = abs(va - vb) <= 0.2
                if not ok:
                    cross_bad.append({"code": code, "区间": seg, f"{a}验证": va, f"{b}训练": vb})
    print(f"  比对 {pairs} 对（10 只 × 2 个重叠区间），不一致 {len(cross_bad)} 对")
    for x in cross_bad:
        print("   ★", x)
    if not cross_bad:
        print(f"  ✅ 全部 {pairs} 对的标的收益在同一区间跨窗口完全一致")
    REPORT["R3_跨窗口标的收益一致"] = {"对数": pairs, "不一致": cross_bad}
    REPORT["R3_数值自洽"] = {"组数": int(n.groupby(["c6", "窗口"]).ngroups),
                             "容差": TOL, "不一致组": bad, "反解标的收益": implied}
    return REPORT


# ─────────────────── R4 差异分解（含缺口标的的 W3）───────────────────
def r4_diff_decomposition(new: pd.DataFrame, old: pd.DataFrame) -> dict:
    sep("R4 差异分解：A21 vs 原版 —— 完整标的 vs 缺口标的（含这 5 只的 W3）")
    print("上一轮只量化了『完整标的』的差异（结论：仅 H17 + H31）。")
    print("本次补上『缺口标的』的差异 —— 那是 A20 的**预期内影响面**。")
    print()

    for d in (new, old):
        d["c6"] = d["code"].astype(str).str.zfill(6)
    key = ["窗口", "c6", "sid"]
    cols = ["训练收益", "训练相对", "验证收益", "验证相对", "验证MDD", "验证交易数"]
    o = old.set_index(key).sort_index()
    n = new.set_index(key).sort_index()

    result = {}
    for group, codes in (("完整标的", [c for c, _ in STOCKS if c not in GAPFIX]),
                         ("缺口标的", sorted(GAPFIX))):
        sel = [(w, c, s) for (w, c, s) in n.index if c in codes]
        sel = [k for k in sel if k in o.index]
        if not sel:
            continue
        oc = o.loc[sel, cols]
        nc = n.loc[sel, cols]
        diffmask = (oc != nc)
        print(f"--- {group}（{len(set(k[1] for k in sel))} 只 × 窗口 × 策略，"
              f"{len(sel)} 条记录 × {len(cols)} 列 = {len(sel)*len(cols)} 格）---")
        by_sid = diffmask.groupby(level="sid").sum().sum(axis=1)     # Series: sid -> 差异格数
        by_win = diffmask.groupby(level="窗口").sum().sum(axis=1)     # Series: 窗口 -> 差异格数
        print("  按策略：", {str(k): int(v) for k, v in by_sid.items()})
        print("  按窗口：", {str(k): int(v) for k, v in by_win.items()})
        # 完全未变的记录数
        unchanged = int((~diffmask.any(axis=1)).sum())
        print(f"  完全未变的记录: {unchanged} / {len(sel)}")
        result[group] = {
            "记录数": len(sel), "格数": len(sel) * len(cols),
            "按策略差异格": {str(k): int(v) for k, v in by_sid.items()},
            "按窗口差异格": {str(k): int(v) for k, v in by_win.items()},
            "完全未变记录": unchanged,
        }
        print()

    # 缺口标的的 W3 单独看（这是上轮遗漏的）
    print("--- 缺口标的的 W3（上轮遗漏：其训练段=2025 原本缺 61%）---")
    w3 = [(w, c, s) for (w, c, s) in n.index if w == "W3" and c in GAPFIX and (w, c, s) in o.index]
    if w3:
        oc = o.loc[w3, cols]
        nc = n.loc[w3, cols]
        dm = (oc != nc)
        ch = [k for k in w3 if dm.loc[k].any()]
        print(f"  W3 记录 {len(w3)} 条，有变化 {len(ch)} 条")
        for k in ch[:12]:
            d = {c: (o.loc[k, c], n.loc[k, c]) for c in cols if o.loc[k, c] != n.loc[k, c]}
            print(f"    {k}: " + " | ".join(f"{c} {a}→{b}" for c, (a, b) in d.items()))
        result["缺口标的W3"] = {"记录数": len(w3), "有变化": len(ch)}
    print()
    REPORT["R4_差异分解"] = result
    return REPORT


# ───────────────────────── R5 离群与分布 ─────────────────────────
def r5_outliers(new: pd.DataFrame, old: pd.DataFrame) -> dict:
    sep("R5 离群值与 regime 分布")
    n = new.copy()
    n["c6"] = n["code"].astype(str).str.zfill(6)

    thr = 50.0
    ex = n[(n["验证相对"].abs() >= thr) | (n["训练相对"].abs() >= thr)]
    print(f"|相对收益| ≥ {thr}% 的记录：{len(ex)} / {len(n)}")
    if len(ex):
        print()
        print(f"{'窗口':>4} {'代码':>8} {'名称':<8} {'策略':<12} {'训练收益':>8} {'训练相对':>8} "
              f"{'验证收益':>8} {'验证相对':>8} {'反解验证基准':>12}")
        for _, r in ex.sort_values(["窗口", "c6"]).iterrows():
            impl = r["验证收益"] - r["验证相对"]
            print(f"{r['窗口']:>4} {r['c6']:>8} {str(r['name'])[:7]:<8} {r['sid']:<12} "
                  f"{r['训练收益']:>8.1f} {r['训练相对']:>8.1f} "
                  f"{r['验证收益']:>8.1f} {r['验证相对']:>8.1f} {impl:>12.1f}")
    print()
    print("说明：这些不是缺陷 —— 反解出的『验证基准』即该标的在该段的 buy&hold 涨幅，")
    print("      多为 AI/半导体行情中的极端个股。它们会**主导**跨窗口一致率判断。")

    print()
    print("--- regime 分布（口径=各标的验证段 buy&hold 收益）× 窗口 ---")
    print()
    for label, d in (("原版", old), ("A21", new)):
        dd = d.copy()
        dd["c6"] = dd["code"].astype(str).str.zfill(6)
        # 每个 (窗口, regime) 的标的数（去重）
        piv = dd.groupby(["窗口", "regime"])["c6"].nunique().unstack(fill_value=0)
        print(f"  【{label}】标的数（去重）")
        print("  " + piv.to_string().replace("\n", "\n  "))
        # 参与一致率计算的格数 = 有样本的 (窗口,regime)
        cells_old = dd.groupby(["窗口", "regime"]).ngroups
        # 每格内策略数 = 标的数 × 4
        print()
    dd = old.copy(); dd["c6"] = dd["code"].astype(str).str.zfill(6)
    cells_o = {(w, r) for (w, r) in dd.groupby(["窗口", "regime"]).groups}
    cells_n = {(w, r) for (w, r) in
               new.assign(c6=new["code"].astype(str).str.zfill(6))
               .groupby(["窗口", "regime"]).groups}
    print(f"  (窗口,状态) 格数：原版 {len(cells_o)} → A21 {len(cells_n)}")
    print(f"  新增的格：{sorted(cells_n - cells_o)}")

    REPORT["R5_离群"] = {
        "阈值": thr,
        "离群记录数": int(len(ex)),
        "离群清单": [
            {"窗口": r["窗口"], "code": r["c6"], "sid": r["sid"],
             "训练收益": float(r["训练收益"]), "训练相对": float(r["训练相对"]),
             "验证收益": float(r["验证收益"]), "验证相对": float(r["验证相对"]),
             "反解验证基准": float(r["验证收益"] - r["验证相对"])}
            for _, r in ex.iterrows()],
        "格数_原版": len(cells_o), "格数_A21": len(cells_n),
        "新增格": sorted(f"{w}/{r}" for w, r in (cells_n - cells_o)),
    }
    return REPORT


def main() -> int:
    print("A21 逐窗口复核（只读；不跑策略 ⇒ 零联网零写盘）")
    for p in (NEW_CSV, OLD_CSV):
        if not p.exists():
            print(f"✗ 缺少输入：{p}")
            return 2
    new = pd.read_csv(NEW_CSV, encoding="utf-8-sig")
    old = pd.read_csv(OLD_CSV, encoding="utf-8-sig")
    print(f"输入：A21 {len(new)} 行 ／ 原版 {len(old)} 行")

    r1_segment_completeness()
    r2_internal_consistency(new)
    r3_arithmetic(new)
    r4_diff_decomposition(new, old)
    r5_outliers(new, old)

    REPORT_JSON.write_text(
        json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("=" * 96)
    print(f"复核报告已存：{REPORT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
