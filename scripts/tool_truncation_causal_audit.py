# -*- coding: utf-8 -*-
"""A2：截断重算因果审计（H16 / C3）—— 监测项，不阻断。

用法：`"D:/Anaconda3/python.exe" scripts/tool_truncation_causal_audit.py [all|1|2|3]`
  · 不带参数 = all（三个 Part 全跑；Part 2 最贵，ma_swing 单跑约 6s/250 天窗口）
  · 单独重跑某一 Part：`… audit.py 1` / `… audit.py 3`

背景
----
`core/adjustment.py:55` 用 `latest = aligned[-1]`，即**「所传 df 最后一根 bar 的复权因子」**
作为前复权基准。因此把 df 截断到 T，`latest` 就从"数据末端因子"变成"T 日因子"，
**共同历史区间的 signal_close 会整体乘以一个常数**。

闭式解（本文档实测验证）：
    偏移倍数 = factor(T) / factor(data_end) = 1 / Π(1 + Δᵢ)
    Δᵢ = 落在 (T, data_end] 区间内的每一个复权事件幅度
    ⇒ 若 T 之后没有任何复权事件，偏移**恰好为 0**。

为什么不能写成"断言无泄漏"的守卫用例
----------------------------------
该断言在真实数据上**必然失败**（切点跨 2024-06-06 时偏移 40.4%）。
要让"偏移 = 0"成立，必须先把 `latest` 改成**逐日推进的时点因子**（= C3 的 PIT 修法）。
在修法落地前，本脚本只做**监测**：把偏移量、以及"偏移是否已经影响到决策"报出来。

本脚本输出三部分
--------------
  Part 1  偏移量分布：全部（标的 × 切点）的解析分布 + 抽验实测（验证解析式）
  Part 2  **决策不变量检验（核心）**：对 signal_* 施加 1+δ 的整体缩放，
          扫描 δ 从 0.001% 到 ±300%，看两条策略的成交与权益是否改变
          ⇒ 给出「多大的锚点偏移才会改变决策」= **报警线应从数据取，而不是拍**
  Part 3  真实截断 A/B：在真实发生过复权事件的切点上，端到端比对两条路径的决策

产物：`test_outputs/truncation_causal_audit/` 下的 CSV 与文本摘要。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "scripts"))

from core.adjustment import load_backward_factor, prepare_signal_prices   # noqa: E402
from strategies import get_strategy                                       # noqa: E402
from tool_exec_latency_ab import normalized_params, CASH                   # noqa: E402

DATA = APP_DIR / "data"
OUT = APP_DIR / "test_outputs" / "truncation_causal_audit"

# Part 2 的扫描档位：δ = 对 signal_* 的整体缩放比例（模拟锚点偏移）
DELTAS = [0.0,
          0.00001, -0.00001,        # ±0.001%
          0.0001, -0.0001,          # ±0.01%
          0.001, -0.001,            # ±0.1%
          0.005, -0.005,            # ±0.5%
          0.01, -0.01,              # ±1%
          0.05, -0.05,              # ±5%
          0.10, -0.10,              # ±10%
          0.50, -0.50,              # ±50%
          1.00, -1.00,              # ±100%
          3.00, -3.00]              # ±300%

# Part 2 用的标的与窗口（窗口 = 数据末段，最贴近当前实盘）
SWEEP_CODES = ["300308", "300502", "688256"]
SWEEP_TAIL_DAYS = 250          # 末段约一年
SWEEP_STRATEGIES = ["grid_trade", "ma_swing"]


def trading_days(df: pd.DataFrame) -> list:
    return sorted(pd.to_datetime(df["time"]).dt.normalize().unique())


def monthly_cuts(df: pd.DataFrame) -> list:
    """每个自然月的第一个交易日。"""
    days = pd.to_datetime(trading_days(df))
    s = pd.Series(days, index=days)
    return list(s.groupby([days.year, days.month]).first().values)


def predicted_bias(factor: pd.Series, cut: pd.Timestamp, end: pd.Timestamp) -> tuple[float, float]:
    """返回 (偏移倍数 = factor(cut)/factor(end), 切点后最大单次事件幅度)。"""
    f = factor.copy()
    f.index = pd.to_datetime(f.index)
    f = f.sort_index()
    fc = float(f.reindex([cut]).ffill().iloc[0])
    fe = float(f.reindex([end]).ffill().iloc[0])
    ratio = fc / fe
    rel = f.pct_change().abs()
    after = rel[(rel > 1e-8) & (f.index > cut) & (f.index <= end)]
    return ratio, (float(after.max()) if len(after) else 0.0)


def load_minute(code: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"stock_{code}_1m.csv")
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)


def run_strategy(sid: str, df: pd.DataFrame, code: str) -> dict:
    s = get_strategy(sid)
    p = normalized_params(sid, df, CASH, 5)
    r = s.run(df.copy(), CASH, p, {"code": code})
    t = r.trades.copy()
    if len(t):
        t["time"] = pd.to_datetime(t["time"])
    else:
        t = pd.DataFrame(columns=["time", "direction", "shares", "price", "fee"])
    return {"trades": t, "final_equity": float(r.final_equity)}


# ----------------------------------------------------------------------------
def part1_distribution() -> pd.DataFrame:
    """全部（标的 × 切点）的解析偏移分布 + 抽验实测。"""
    print("=" * 96)
    print("Part 1  锚点偏移：解析分布 + 抽验实测")
    print("=" * 96)
    recs = []
    codes = sorted(p.stem.replace("stock_", "").replace("_1m", "")
                   for p in DATA.glob("stock_*_1m.csv"))
    for code in codes:
        fpath = DATA / f"factor_{code}.csv"
        if not fpath.exists():
            continue
        f = pd.read_csv(fpath)
        factor = pd.Series(f["factor"].astype(float).values,
                           index=pd.to_datetime(f["date"]).dt.normalize()).sort_index()
        # 数据末段（只需末日期，避免整表读取）
        t = pd.to_datetime(pd.read_csv(DATA / f"stock_{code}_1m.csv", usecols=["time"])["time"])
        end = t.max().normalize()
        dd = pd.DatetimeIndex(sorted(pd.unique(t.dt.normalize())))
        cuts = pd.Series(dd, index=dd).groupby([dd.year, dd.month]).first().tolist()
        for c in cuts:
            ratio, maxev = predicted_bias(factor, pd.Timestamp(c), end)
            # ⚠️ 偏移的定义 = 两臂 signal_close 的相对差 = |1/ratio − 1|
            #    （ratio = factor(cut)/factor(end)；signal_f/signal_t = ratio
            #      ⇒ |f−t|/f = |1 − t/f| = |1 − 1/ratio|）
            #    这里曾把方向写成 |ratio−1|，被 Part 3 的实测值打脸后更正。
            recs.append({"code": code, "cut": pd.Timestamp(c).date(),
                         "ratio": ratio, "bias_pct": abs(1.0 / ratio - 1.0) * 100,
                         "max_event_after_pct": maxev * 100})
    d = pd.DataFrame(recs)
    d.to_csv(OUT / "part1_bias_distribution.csv", index=False, encoding="utf-8-sig")

    print(f"  覆盖 {d['code'].nunique()} 只标的、{len(d)} 个（标的×切点）组合")
    zero = (d["bias_pct"].abs() < 1e-9).sum()
    print(f"  偏移恰好为 0 的组合：{zero} / {len(d)}  = {zero/len(d)*100:.1f}%"
          f"   （这些切点之后没有任何复权事件）")
    nz = d[d["bias_pct"].abs() > 1e-9]
    print(f"  偏移非 0 的 {len(nz)} 个组合，|偏移| 分位数：")
    for q in [0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99, 1.00]:
        print(f"      p{int(q*100):02d} = {nz['bias_pct'].abs().quantile(q):8.3f}%")
    print()
    print("  |偏移| 分档计数（非零组）：")
    a = nz["bias_pct"].abs()
    for lo, hi in [(0, 0.1), (0.1, 0.5), (0.5, 1), (1, 5), (5, 20), (20, 50), (50, 1e9)]:
        n = ((a >= lo) & (a < hi)).sum()
        if n:
            print(f"      {lo:>5.1f}% ~ {hi:<6.1f}%  {n:4d} 组")
    print()
    print("  最大 8 个偏移：")
    print(nz.reindex(nz["bias_pct"].abs().sort_values(ascending=False).index)
          .head(8).to_string(index=False))

    # ---- 抽验：解析式 vs 实测 ----
    print()
    print("  ---- 抽验：解析式 vs 实测（prepare_signal_prices 真实跑）----")
    probe = [("300308", "2024-06-01"), ("300308", "2026-04-01"),
             ("300308", "2025-01-01"), ("688256", "2026-06-01"),
             ("300502", "2025-01-01"), ("002594", "2025-01-01")]
    chk = []
    for code, cut_s in probe:
        df = load_minute(code)
        cut = pd.Timestamp(cut_s)
        full, _, _ = prepare_signal_prices(df, code)
        trunc, _, _ = prepare_signal_prices(df[df["time"] <= cut].copy(), code)
        k = full.merge(trunc, on="time", suffixes=("_f", "_t"))
        k = k[k["time"] <= cut]
        rel = ((k["signal_close_f"] - k["signal_close_t"]).abs() / k["signal_close_f"])
        measured = float(rel.max()) if len(k) else float("nan")
        spread = float(rel.max() - rel.min()) if len(k) else float("nan")
        # 偏移 = |1 − t/f|；从首根 bar 的比值反推（⚠️ 不是 |f/t − 1|）
        r0 = float(k["signal_close_t"].iloc[0] / k["signal_close_f"].iloc[0]) if len(k) else float("nan")
        pred_bias = abs(r0 - 1.0)
        tol = 1e-6 * max(measured, pred_bias, 1e-12)
        chk.append({"code": code, "cut": cut_s, "bars": len(k),
                    "measured_max_rel_pct": measured * 100,
                    "from_ratio_pct": pred_bias * 100,
                    "rel_spread_pct": spread * 100,
                    "match": abs(measured - pred_bias) <= tol})
        print(f"    {code} 切点 {cut_s}: {len(k):6d} bars  实测最大相对偏移 "
              f"{measured*100:9.4f}%  解析式 {pred_bias*100:9.4f}%  "
              f"bar间离差 {spread*100:.2e}%  "
              f"{'✅ 一致' if chk[-1]['match'] else '❌ 不一致'}")
    pd.DataFrame(chk).to_csv(OUT / "part1_verification.csv", index=False, encoding="utf-8-sig")
    return d


# ----------------------------------------------------------------------------
def part2_scale_invariance() -> pd.DataFrame:
    """对 signal_* 施加整体缩放，看策略决策是否改变 ⇒ 求"报警线"。"""
    print()
    print("=" * 96)
    print("Part 2  决策不变量检验：多大的锚点偏移才会改变决策？")
    print("=" * 96)
    recs = []
    for code in SWEEP_CODES:
        df = load_minute(code)
        base, _, _ = prepare_signal_prices(df, code)
        days = sorted(pd.to_datetime(base["time"]).dt.normalize().unique())
        if len(days) > SWEEP_TAIL_DAYS:
            lo = days[-SWEEP_TAIL_DAYS]
            base = base[base["time"] >= lo].reset_index(drop=True)
        print(f"  -- {code}: 窗口 {base['time'].min().date()} ~ {base['time'].max().date()}"
              f"（{base['time'].dt.normalize().nunique()} 个交易日 / {len(base)} bars）")
        for sid in SWEEP_STRATEGIES:
            ref = run_strategy(sid, base, code)
            ref_t = ref["trades"]
            key_ref = (ref_t["time"].astype(str) + "|" + ref_t["direction"].astype(str)
                       + "|" + ref_t["shares"].astype(str)).tolist()
            first_change = None
            for delta in DELTAS:
                sc = base.copy()
                if delta != 0.0:
                    for col in ("signal_open", "signal_high", "signal_low", "signal_close"):
                        sc[col] = sc[col].astype(float) * (1.0 + delta)
                got = run_strategy(sid, sc, code)
                gt = got["trades"]
                key = (gt["time"].astype(str) + "|" + gt["direction"].astype(str)
                       + "|" + gt["shares"].astype(str)).tolist()
                eq_diff = got["final_equity"] - ref["final_equity"]
                recs.append({"code": code, "strategy": sid, "delta_pct": delta * 100,
                             "n_trades_ref": len(ref_t), "n_trades": len(gt),
                             "trades_identical": key == key_ref,
                             "equity_diff": eq_diff,
                             "equity_diff_pct": eq_diff / ref["final_equity"] * 100})
                if key != key_ref and first_change is None:
                    first_change = delta
            if first_change is None:
                print(f"     {sid:12s} 成交笔数 {len(ref_t):3d}："
                      f"**在 ±300% 全档位内决策完全不变** ⇒ 对整体缩放不敏感")
            else:
                print(f"     {sid:12s} 成交笔数 {len(ref_t):3d}："
                      f"**首次改变发生在 δ = {first_change*100:+.4f}%**")
    d = pd.DataFrame(recs)
    d.to_csv(OUT / "part2_scale_invariance.csv", index=False, encoding="utf-8-sig")
    return d


# ----------------------------------------------------------------------------
def part3_real_cut_ab() -> pd.DataFrame:
    """真实截断 A/B：全局锚定 vs 期末锚定，在同一段历史上比对决策。"""
    print()
    print("=" * 96)
    print("Part 3  真实截断 A/B（端到端）：全局锚定 vs 期末锚定")
    print("=" * 96)
    recs = []
    cases = [("300308", "2024-06-01"), ("300308", "2025-01-01"),
             ("300308", "2026-04-01"), ("300308", "2026-08-01"),
             ("688256", "2026-06-01"), ("300502", "2025-01-01")]
    for code, cut_s in cases:
        df = load_minute(code)
        cut = pd.Timestamp(cut_s)
        prep_full, _, _ = prepare_signal_prices(df, code)
        armF = prep_full[prep_full["time"] <= cut].reset_index(drop=True)
        armT, _, _ = prepare_signal_prices(df[df["time"] <= cut].copy(), code)
        if not len(armF) or not len(armT):
            continue
        rel = ((armF["signal_close"] - armT["signal_close"]).abs() / armF["signal_close"])
        price_bias = float(rel.max())
        n_days = pd.to_datetime(armF["time"]).dt.normalize().nunique()
        for sid in SWEEP_STRATEGIES:
            try:
                a = run_strategy(sid, armF, code)
                b = run_strategy(sid, armT, code)
            except Exception as e:
                print(f"    {code} {cut_s} {sid}: 跳过（{type(e).__name__}: {e}）")
                continue
            ka = (a["trades"]["time"].astype(str) + "|" + a["trades"]["direction"].astype(str)
                  + "|" + a["trades"]["shares"].astype(str)).tolist()
            kb = (b["trades"]["time"].astype(str) + "|" + b["trades"]["direction"].astype(str)
                  + "|" + b["trades"]["shares"].astype(str)).tolist()
            same = ka == kb
            recs.append({"code": code, "cut": cut_s, "strategy": sid, "bars": len(armF),
                         "days": n_days, "price_bias_pct": price_bias * 100,
                         "n_trades_global_anchor": len(a["trades"]),
                         "n_trades_period_anchor": len(b["trades"]),
                         "trades_identical": same,
                         "equity_diff": a["final_equity"] - b["final_equity"],
                         "equity_diff_pct": (a["final_equity"] - b["final_equity"])
                                            / b["final_equity"] * 100})
            print(f"    {code} {cut_s} [{sid:11s}] bars={len(armF):6d} 信号价偏移="
                  f"{price_bias*100:7.3f}%  成交 {len(a['trades']):3d} vs {len(b['trades']):3d}  "
                  f"{'✅ 决策一致' if same else '❌ 决策不同'}  权益差 "
                  f"{recs[-1]['equity_diff']:+.4f} 元 ({recs[-1]['equity_diff_pct']:+.6f}%)")
    d = pd.DataFrame(recs)
    d.to_csv(OUT / "part3_real_cut_ab.csv", index=False, encoding="utf-8-sig")
    return d


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sel = sys.argv[1] if len(sys.argv) > 1 else "all"
    do = lambda k: (sel == "all") or (k in sel)          # noqa: E731
    print(f"运行档位：{sel}   (可选 all / 1 / 2 / 3 / 12 / 23 ...)\n")

    d1 = part1_distribution() if do("1") else pd.DataFrame()
    d2 = part2_scale_invariance() if do("2") else pd.DataFrame()
    d3 = part3_real_cut_ab() if do("3") else pd.DataFrame()

    print()
    print("=" * 96)
    print("结论摘要")
    print("=" * 96)
    if len(d1):
        zero = (d1["bias_pct"].abs() < 1e-9).sum()
        nz = d1[d1["bias_pct"].abs() > 1e-9]
        print(f"  1) 锚点偏移有闭式解：offset = |1 − 1/Π(1+Δᵢ)|，Δᵢ = 切点之后的全部复权事件。")
        print(f"     全部 {len(d1)} 个（标的×切点）组合中，{zero} 个偏移**恰好为 0**"
              f"（{zero/len(d1)*100:.1f}%）—— 这些切点之后没有任何复权事件。")
        if len(nz):
            print(f"     非零组 {len(nz)} 个：中位数 {nz['bias_pct'].abs().median():.2f}%，"
                  f"最大 {nz['bias_pct'].abs().max():.2f}%。")
    if len(d2):
        changed = d2[~d2["trades_identical"]]
        n_run = len(d2)
        print(f"  2) 决策不变量检验（{n_run} 组 = {d2['code'].nunique()} 标的 × "
              f"{d2['strategy'].nunique()} 策略 × {d2['delta_pct'].nunique()} 档位，"
              f"共 {int(d2['n_trades_ref'].sum()/d2['delta_pct'].nunique())} 笔基准成交）：")
        if len(changed) == 0:
            print("     **成交序列逐笔完全不变** ⇒ 锚点偏移对**决策**零影响。")
            print("     ⇒ H16 的风险因此收敛为一个**代码不变量**：")
            print("        只要 signal_* 仅参与尺度不变的比较，偏移多大都不影响决策；")
            print("        反之，任何「用信号价与真实价直接比较」的新代码都会立刻破坏它。")
        else:
            mn = changed["delta_pct"].abs().min()
            print(f"     **首次改变发生在 δ = ±{mn:.4f}%** ⇒ 报警线候选（从数据取，非直觉拍）。")
    if len(d3):
        bad = d3[~d3["trades_identical"]]
        print(f"  3) 真实截断 A/B：{len(d3)} 组中决策不同 {len(bad)} 组、一致 {len(d3)-len(bad)} 组；"
              f"权益差最大 {d3['equity_diff'].abs().max():.4f} 元。")
    print()
    print(f"  产物目录：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
