# -*- coding: utf-8 -*-
"""A21：滚动窗口样本外验证的**重跑**（A20 补数据之后）。

⚠️ 忠实性原则
--------------
本脚本的**窗口定义 / 标的清单 / 策略参数 / 过滤条件**全部**逐行照搬**原生成脚本
`D:\\Codex输出\\_tmp_rolling_oos.py`（2026-09-14 15:05），**不做任何"改进"** ——
否则重跑结果与历史结果**不可比**，就失去"A20 到底带来多少收益"的判断力。
唯一差异：**输出路径**改为新目录，**不覆盖**原明细（原文件已备份）。

原版的关键过滤（正是 W1/W2 归零的机制）
--------------------------------------
    if df.empty or df["time"].dt.date.nunique() < 40:
        return None          # 该段少于 40 个交易日 ⇒ 整条 (窗口,标的,策略) 记录被丢弃
⇒ 缺口标的在 W1 的"验证段=2024"只有 0–28 天 ⇒ `te=None` ⇒ 整条被丢。

验收点（用户指定）
------------------
1. **窗口行数恢复**：W1/W2 中 5 只缺口标的应从 0 行恢复到 **40 行**（= 5 只 × 2 窗口 × 4 策略）
2. **原有结果不变**：未涉及这 5 只的窗口（即另外 5 只完整标的），结果应与原版**完全一致**
3. 引用结论更新：**不自动解除**标注（由报告侧决定）

用法
----
    D:\\Anaconda3\\python.exe -X utf8 scripts/tool_rolling_oos_rerun.py
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402

from core.data import load_data, stock_name  # noqa: E402
from strategies import get_strategy  # noqa: E402

OUT_DIR = Path(r"D:\Codex输出\A21滚动窗口重跑")
OUT_CSV = OUT_DIR / "滚动窗口样本外验证_明细_A21.csv"
OLD_CSV = OUT_DIR / "滚动窗口样本外验证_明细_原版备份.csv"

# ── 以下与 _tmp_rolling_oos.py 完全一致 ──
STOCKS = [
    ("688256", "寒武纪"), ("300308", "中际旭创"), ("300502", "新易盛"), ("300418", "昆仑万维"),
    ("300364", "中文在线"), ("002371", "北方华创"), ("601619", "嘉泽新能"), ("000572", "海马汽车"),
    ("600900", "长江电力"), ("002594", "比亚迪"),
]
STRATEGY_IDS = ["intraday_t", "grid_trade", "sentiment_t", "ma_swing"]

WINDOWS = [
    ("W1", "2023-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("W2", "2024-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
    ("W3", "2025-01-01", "2025-12-31", "2026-01-01", "2026-08-21"),
]

GAPFIX = {"000572", "002371", "300364", "300502", "601619"}   # A20 补过的 5 只


def regime(r):
    if pd.isna(r):
        return "未知"
    if r <= -20:
        return "深跌"
    if r < -5:
        return "下跌"
    if r <= 5:
        return "震荡"
    if r <= 30:
        return "温和上涨"
    return "大涨"


def run_strategy(sid, df, code):
    s = get_strategy(sid)
    params = s.default_params()
    init = 1_000_000.0
    if sid == "ma_swing":
        params["position_pct"] = 30.0
    else:
        fp = float(df["close"].iloc[0])
        base = int(init * 0.30 / fp) // 100 * 100
        if base < 100:
            return None
        params["base_position"] = base
        if "fixed_shares" in params:
            params["fixed_shares"] = max(100, int(base * 0.1) // 100 * 100)
        if "trade_shares" in params:
            params["trade_shares"] = max(100, int(base * 0.1) // 100 * 100)
        params["max_position"] = max(base * 2, base + 100)
    r = s.run(df, init, params, {"code": code})
    return {"ret": r.total_return * 100, "mdd": r.max_drawdown * 100, "trades": len(r.trades)}


def run_period(code, sid, start, end):
    df, _ = load_data(code, start, end)
    if df.empty or df["time"].dt.date.nunique() < 40:
        return None
    stock_ret = float(df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    try:
        res = run_strategy(sid, df, code)
        if res is None:
            return None
        res["stock_ret"] = stock_ret
        res["relative"] = res["ret"] - stock_ret
        res["regime"] = regime(stock_ret)
        return res
    except Exception:
        return None


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    total = len(WINDOWS) * len(STOCKS) * len(STRATEGY_IDS)
    done = 0
    for wname, tr_s, tr_e, te_s, te_e in WINDOWS:
        for code, name in STOCKS:
            for sid in STRATEGY_IDS:
                done += 1
                tr = run_period(code, sid, tr_s, tr_e)
                te = run_period(code, sid, te_s, te_e)
                if tr and te:
                    rows.append({
                        "窗口": wname, "code": code, "name": name, "sid": sid,
                        "regime": te["regime"],
                        "训练收益": round(tr["ret"], 1), "训练相对": round(tr["relative"], 1),
                        "验证收益": round(te["ret"], 1), "验证相对": round(te["relative"], 1),
                        "验证MDD": round(te["mdd"], 1), "验证交易数": te["trades"],
                    })
                print(f"[{done}/{total}] {wname} {code} {sid} "
                      f"{'✓' if (tr and te) else '×丢弃'}", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    print()
    print(f"总行数: {len(df)}, 窗口: {df['窗口'].nunique()}, "
          f"股票: {df['code'].nunique()}, 策略: {df['sid'].nunique()}")
    print(f"明细已存: {OUT_CSV}")

    # ────────── 验收点 1 & 2 ──────────
    print()
    print("=" * 100)
    print("【验收点 1】窗口行数恢复（按 (标的,窗口) 统计；不能只看总行数）")
    print("=" * 100)
    old = pd.read_csv(OLD_CSV, encoding="utf-8-sig")
    new = pd.read_csv(OUT_CSV, encoding="utf-8-sig")

    def piv(d):
        p = d.pivot_table(index="code", columns="窗口", values="sid", aggfunc="count", fill_value=0)
        for w in ("W1", "W2", "W3"):
            if w not in p.columns:
                p[w] = 0
        return p[["W1", "W2", "W3"]]

    po, pn = piv(old), piv(new)
    codes = sorted(set(po.index) | set(pn.index), key=lambda c: str(c).zfill(6))
    print(f"{'代码':>8} {'类别':>10}   {'W1':>10} {'W2':>10} {'W3':>10}   行数变化")
    print("-" * 74)
    for c in codes:
        o = po.loc[c] if c in po.index else pd.Series({"W1": 0, "W2": 0, "W3": 0})
        n = pn.loc[c] if c in pn.index else pd.Series({"W1": 0, "W2": 0, "W3": 0})
        cat = "❌缺口(已补)" if str(c).zfill(6) in GAPFIX else "✅完整"
        print(f"{str(c).zfill(6):>8} {cat:>12}   "
              f"{int(o['W1'])}→{int(n['W1']):<6} {int(o['W2'])}→{int(n['W2']):<6} {int(o['W3'])}→{int(n['W3']):<6} "
              f"{len(old[old['code'] == c])}→{len(new[new['code'] == c])}")

    gap_w12_new = int(pn.loc[[c for c in pn.index if str(c).zfill(6) in GAPFIX], ["W1", "W2"]].to_numpy().sum())
    print()
    print(f"缺口标的在 W1+W2 的行数：**{gap_w12_new} 行**（原版 0 行，预期 40 行）"
          f"  ⇒ {'✅ 验收点1 通过' if gap_w12_new == 40 else '❌ 与预期不符'}")

    print()
    print("=" * 100)
    print("【验收点 2】原有结果不变（未涉及补数据的 5 只完整标的应完全一致）")
    print("=" * 100)
    key = ["窗口", "code", "sid"]
    cols = ["训练收益", "训练相对", "验证收益", "验证相对", "验证MDD", "验证交易数"]
    om = old.set_index(key).sort_index()
    nm = new.set_index(key).sort_index()
    common = om.index.intersection(nm.index)
    o_c = om.loc[common].sort_index()
    n_c = nm.loc[common].sort_index()

    intact = [c for c in codes if str(c).zfill(6) not in GAPFIX]
    m_intact = [i for i in common if str(i[1]).zfill(6) in {str(x).zfill(6) for x in intact}]
    diff_intact = (o_c.loc[m_intact, cols] != n_c.loc[m_intact, cols]).sum().sum()
    print(f"完整标的（{len(intact)} 只）共有 {len(m_intact)} 条可比记录")
    print(f"  ⇒ 数值不一致的单元格数：**{diff_intact}**   "
          f"{'✅ 验收点2 通过（逐格一致）' if diff_intact == 0 else '❌ 有差异，需查因'}")

    m_gap = [i for i in common if str(i[1]).zfill(6) in {x.zfill(6) for x in GAPFIX}]
    if m_gap:
        diff_gap = (o_c.loc[m_gap, cols] != n_c.loc[m_gap, cols]).sum().sum()
        tot_gap = len(m_gap) * len(cols)
        print(f"缺口标的（W3 段，两版都有）共有 {len(m_gap)} 条记录："
              f"变化单元格 {diff_gap}/{tot_gap}  ⇒ 预期会变（2026 段也被补全）")
    print()
    print("=" * 100)
    print("【验收点 3】引用结论的标注：**本脚本不解除任何标注** ——")
    print("  需对应报告重新生成并复核后才可由报告侧解除（A21）。")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
