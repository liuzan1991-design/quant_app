# -*- coding: utf-8 -*-
"""A21 复核 · 格级归因：拆解跨窗口一致率 3/11 → 2/13 的机制（只读）。

用户特别点出：**不要笼统归因"A21 加的样本本身更极端"，要拆到具体哪几格、因什么而变。**

本脚本回答三个问题
------------------
Q1 原版命中的是哪 3 格？A21 命中的是哪 2 格？
Q2 有哪几格**命中状态发生了变化**？分别是：
     · 新增格（原本不存在 → 现存在）
     · 原有格的标的构成变化（缺口标的被补进来）
     · 原有格内数值变化（W3 的 5 只，其训练段 2025 被补全）
Q3 每一格里，"训练段最优"与"验证段最优"分别是谁、差多少？

另附 D2 补强：新增交易日是否 == 参考日历在 [首日, 新末根] 内该标的原本缺的交易日。

用法
----
    D:\\Anaconda3\\python.exe -X utf8 scripts\\tool_a21_cell_attribution.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import pandas as pd  # noqa: E402

OUT_DIR = Path(r"D:\Codex输出\A21滚动窗口重跑")
NEW_CSV = OUT_DIR / "滚动窗口样本外验证_明细_A21.csv"
OLD_CSV = OUT_DIR / "滚动窗口样本外验证_明细_原版备份.csv"
BAK_DIR = Path(r"C:\Users\27329\quant_app_backups\data_gapfix_20260920_181651")
CUR_DIR = APP_DIR / "data"
OUT_JSON = APP_DIR / "test_outputs" / "a21_cell_attribution.json"

GAPFIX = {"000572", "002371", "300364", "300502", "601619"}
REGIMES = ["深跌", "下跌", "震荡", "温和上涨", "大涨"]
WINDOWS = ["W1", "W2", "W3"]
STOCKS = ["000572", "002371", "300364", "300502", "601619"]


def load(p: Path) -> pd.DataFrame:
    d = pd.read_csv(p, encoding="utf-8-sig")
    d["c6"] = d["code"].astype(str).str.zfill(6)
    return d


def cell_winner(sub: pd.DataFrame) -> tuple[str | None, str | None, dict]:
    """返回 (训练段最优策略, 验证段最优策略, 各策略均值表)。"""
    if sub.empty:
        return None, None, {}
    t = sub.groupby("sid")["训练相对"].mean()
    v = sub.groupby("sid")["验证相对"].mean()
    det = {s: {"训练相对": round(float(t.get(s, float("nan"))), 1),
               "验证相对": round(float(v.get(s, float("nan"))), 1)} for s in sorted(sub["sid"].unique())}
    return t.idxmax(), v.idxmax(), det


def main() -> int:
    new, old = load(NEW_CSV), load(OLD_CSV)
    R: dict = {}

    print("A21 复核 · 格级归因（只读）")
    print(f"输入：A21 {len(new)} 行 ／ 原版 {len(old)} 行")

    # ────────── Q1/Q2/Q3 格级对比 ──────────
    print()
    print("=" * 104)
    print("Q1/Q2/Q3 逐格对比（格 = 窗口 × 市场状态；标的最优 = 该格内各策略『相对收益』均值最高者）")
    print("=" * 104)

    cells_o = {(w, r) for (w, r) in old.groupby(["窗口", "regime"]).groups}
    cells_n = {(w, r) for (w, r) in new.groupby(["窗口", "regime"]).groups}
    allc = sorted(cells_o | cells_n, key=lambda x: (WINDOWS.index(x[0]), REGIMES.index(x[1])))

    hit_o_list, hit_n_list = [], []
    detail = []
    print()
    print(f"{'窗口':>4} {'状态':<6} {'标的(旧→新)':>11} {'训练最优旧→新':>26} "
          f"{'验证最优旧→新':>26} {'命中旧→新':>10}  变化")
    print("-" * 104)
    for (w, rg) in allc:
        o = old[(old["窗口"] == w) & (old["regime"] == rg)]
        n = new[(new["窗口"] == w) & (new["regime"] == rg)]
        tb_o, vb_o, det_o = cell_winner(o)
        tb_n, vb_n, det_n = cell_winner(n)
        ho = (tb_o == vb_o) and tb_o is not None
        hn = (tb_n == vb_n) and tb_n is not None
        # ⚠️ 注意：必须是「命中的格」，不是「有样本的格」（首次实现写错过）
        if ho:
            hit_o_list.append((w, rg))
        if hn:
            hit_n_list.append((w, rg))

        co = sorted(set(o["c6"])) if len(o) else []
        cn = sorted(set(n["c6"])) if len(n) else []
        added = sorted(set(cn) - set(co))
        removed = sorted(set(co) - set(cn))

        # 变化标签
        tags = []
        if not co:
            tags.append("新增格")
        if added:
            tags.append("+标的" + "/".join(added))
        if removed:
            tags.append("-标的" + "/".join(removed))
        if co and not added and not removed and det_o and det_n:
            # 同集合但数值变了？
            ch = [s for s in det_o if s in det_n and det_o[s] != det_n[s]]
            if ch:
                tags.append("同集合数值变")

        # 命中状态变化
        if ho and not hn:
            tags.append("★命中丢失")
        elif hn and not ho:
            tags.append("☆命中新增")

        print(f"{w:>4} {rg:<6} {len(co):>4}→{len(cn):<4} "
              f"{(str(tb_o)+'→'+str(tb_n)):>26} {(str(vb_o)+'→'+str(vb_n)):>26} "
              f"{(('是' if ho else '否')+'→'+('是' if hn else '否')):>10}  {'; '.join(tags)}")

        detail.append({
            "窗口": w, "状态": rg,
            "标的数_旧": len(co), "标的数_新": len(cn),
            "标的_旧": co, "标的_新": cn,
            "标的_新增": added, "标的_移除": removed,
            "训练最优_旧": tb_o, "训练最优_新": tb_n,
            "验证最优_旧": vb_o, "验证最优_新": vb_n,
            "命中_旧": bool(ho), "命中_新": bool(hn),
            "各策略_旧": det_o, "各策略_新": det_n,
        })

    print()
    print(f"原版：命中 {len(hit_o_list)}/{len(cells_o)} = {len(hit_o_list)/len(cells_o)*100:.0f}%"
          f"   [{', '.join(f'{w}/{r}' for w, r in hit_o_list)}]")
    print(f"A21 ：命中 {len(hit_n_list)}/{len(cells_n)} = {len(hit_n_list)/len(cells_n)*100:.0f}%"
          f"   [{', '.join(f'{w}/{r}' for w, r in hit_n_list)}]")

    lost = [(w, r) for (w, r) in hit_o_list if (w, r) not in hit_n_list]
    gained = [(w, r) for (w, r) in hit_n_list if (w, r) not in hit_o_list]
    print()
    print(f"命中丢失的格：{lost if lost else '无'}")
    print(f"命中新增的格：{gained if gained else '无'}")
    print(f"新增的格（原版无样本）：{sorted(cells_n - cells_o)}")
    print(f"消失的格（原版有、A21 无）：{sorted(cells_o - cells_n) if (cells_o - cells_n) else '无'}")

    R["格级"] = detail
    R["命中_旧"] = [f"{w}/{r}" for w, r in hit_o_list]
    R["命中_新"] = [f"{w}/{r}" for w, r in hit_n_list]
    R["命中丢失"] = [f"{w}/{r}" for w, r in lost]
    R["命中新增"] = [f"{w}/{r}" for w, r in gained]

    # ────────── 关键格深挖 ──────────
    print()
    print("=" * 104)
    print("关键格深挖：只看『会影响一致率』的格 —— 命中状态变化 / 新增格 / 标的集合变化")
    print("=" * 104)
    for d in detail:
        focus = (d["命中_旧"] != d["命中_新"]) or bool(d["标的_新增"]) \
            or (d["标的数_旧"] != d["标的数_新"])
        if not focus:
            continue
        w, rg = d["窗口"], d["状态"]
        print()
        print(f"── {w} / {rg}   标的 {d['标的数_旧']} → {d['标的数_新']}"
              f"   新增 {d['标的_新增']}   命中 {d['命中_旧']} → {d['命中_新']}")
        print(f"     旧标的: {d['标的_旧']}   （★=缺口标的）")
        print(f"     新标的: {d['标的_新']}")
        sids = sorted(set(d["各策略_旧"]) | set(d["各策略_新"]))

        def g(dic: dict, k: str) -> float:
            v = dic.get(k)
            return float(v) if v is not None else float("nan")

        recs = []
        for s in sids:
            oo = d["各策略_旧"].get(s, {})
            nn = d["各策略_新"].get(s, {})
            recs.append({"策略": s,
                         "训练相对_旧": g(oo, "训练相对"), "训练相对_新": g(nn, "训练相对"),
                         "验证相对_旧": g(oo, "验证相对"), "验证相对_新": g(nn, "验证相对")})
        df = pd.DataFrame(recs).set_index("策略").astype(float)
        for a, b in (("训练相对_旧", "训练相对_新"), ("验证相对_旧", "验证相对_新")):
            df["Δ" + a.split("_")[0]] = (df[b] - df[a]).round(1)
        df["旧最优"] = ""
        df.loc[d["训练最优_旧"], "旧最优"] = "训练★" if d["训练最优_旧"] else ""
        df.loc[d["验证最优_旧"], "旧最优"] = (df.loc[d["验证最优_旧"], "旧最优"] + "验证★").strip()
        df["新最优"] = ""
        df.loc[d["训练最优_新"], "新最优"] = "训练★" if d["训练最优_新"] else ""
        df.loc[d["验证最优_新"], "新最优"] = (df.loc[d["验证最优_新"], "新最优"] + "验证★").strip()
        print(df.to_string())

    # ────────── D2 补强 ──────────
    print()
    print("=" * 104)
    print("D2 补强：新增交易日 是否 == 参考日历在 [首日, 新末根] 内该标的原本缺的交易日")
    print("=" * 104)
    ref = pd.read_csv(CUR_DIR / "stock_300308_1m.csv", usecols=["time"], parse_dates=["time"])
    ref_days = set(pd.to_datetime(ref["time"]).dt.normalize().unique())
    print(f"参考日历（300308）到数据末根 = {len(ref_days)} 个交易日，末根 {max(ref_days).date()}")
    print()
    print(f"{'代码':>8} {'新增交易日':>10} {'原本缺(到新末根)':>17} {'相等?':>7} {'原末根':>12} {'新末根':>12}")
    print("-" * 104)
    d2rows = []
    for c in STOCKS:
        old_d = pd.read_csv(BAK_DIR / f"stock_{c}_1m.csv", usecols=["time"], parse_dates=["time"])
        new_d = pd.read_csv(CUR_DIR / f"stock_{c}_1m.csv", usecols=["time"], parse_dates=["time"])
        od = set(pd.to_datetime(old_d["time"]).dt.normalize().unique())
        nd = set(pd.to_datetime(new_d["time"]).dt.normalize().unique())
        added = sorted(nd - od)
        lo, hi_new = min(od), max(nd)
        expect = sorted({d for d in ref_days if lo <= d <= hi_new} - od)
        eq = set(added) == set(expect)
        print(f"{c:>8} {len(added):>10} {len(expect):>17} {str(eq):>7} "
              f"{str(max(od).date()):>12} {str(hi_new.date()):>12}")
        d2rows.append({"code": c, "新增交易日": len(added), "原本缺失": len(expect),
                       "相等": bool(eq), "原末根": str(max(od).date()),
                       "新末根": str(hi_new.date())})
    n_eq = sum(1 for r in d2rows if r["相等"])
    print()
    print(f"⇒ {n_eq}/{len(d2rows)} 只：新增交易日**恰好**等于『参考日历在 [该标的原首日, 新末根] 内原本缺的交易日』")
    print("   ⇒ 即 A20 补的是『该标的自身覆盖区间内的全部缺口』，**不止中段，也含尾部缺口**")
    R["D2_补强"] = d2rows

    OUT_JSON.write_text(json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"报告已存：{OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
