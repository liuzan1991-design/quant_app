# -*- coding: utf-8 -*-
"""H32 时间穿越的**端到端**影响实测（只读 + 写盘自证）。

背景
----
`core/sentiment_data.py:139-147`：命中缓存时**直接 `return existing`（未裁剪全量）**，
而 `compute_sector_status`（`:175-188`）取 `close.iloc[-1]` 判断"板块当前强弱"
⇒ 用**缓存末行（2026-09）**去决定 **2023/2024 年**的交易。

静态归因（`tool_h32_lookahead_attribution.py`）已算出 62 例中 **28 例**跨 H24 等价类。
本脚本回答最后一问：**这 28 例的 sector 差异，是否真的改变了回测数字？**

方法
----
同一 (标的, 窗口) 跑两次，**只改 `load_industry_daily` 一个变量**：
- A 组「生产口径」：返回**未裁剪全量**（复现 `:147`）
- B 组「修正口径」：返回**裁剪到回测区间末**（`df[df.time <= end]`）

其余全部相同（`load_index_min` 走真实缓存、参数相同、数据相同）。

安全
----
- `load_industry_daily` 被替换 ⇒ **不触发写盘**（这是 H31 的机理）。
- `load_index_min` 命中缓存时也**不写盘**（`:48` 早退）。脚本用 sha256 自证。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import data as core_data          # noqa: E402
from core import sentiment_data             # noqa: E402
from strategies.sentiment_t import SentimentTStrategy  # noqa: E402

DATA = APP_DIR / "data"

# 全部「真影响」组合（取自静态归因 tool_h32_lookahead_attribution.py 的清单，28 例）
LONG_P = {"2023": ("2023-01-01", "2023-12-29"),
          "2024": ("2024-01-02", "2024-12-31"),
          "2025": ("2025-01-01", "2025-12-31"),
          "2026YTD": ("2026-01-01", "2026-08-21"),
          "全周期": ("2023-01-01", "2026-08-21")}
ROLL_P = {"滚动12月_202302": ("2023-02-01", "2024-01-31"),
          "滚动12月_202308": ("2023-08-01", "2024-07-31"),
          "滚动12月_202402": ("2024-02-01", "2025-01-31"),
          "滚动12月_202408": ("2024-08-01", "2025-07-31"),
          "滚动12月_202502": ("2025-02-01", "2026-01-31"),
          "滚动12月_202508": ("2025-08-01", "2026-07-31"),
          "样本外2025": ("2025-01-01", "2025-12-31"),
          "样本外2026YTD": ("2026-01-01", "2026-08-21")}

_SPEC = {
    "000572": [("2023", "L")],
    "002371": [("2024", "L"), ("2025", "L")],
    "002594": [("2023", "L")],
    "300308": [("2023", "L"), ("2026YTD", "L"), ("全周期", "L"),
               ("样本外2026YTD", "R"), ("滚动12月_202308", "R"),
               ("滚动12月_202502", "R"), ("滚动12月_202508", "R")],
    "300364": [("2023", "L"), ("2025", "L")],
    "300418": [("2023", "L"), ("2025", "L")],
    "300502": [("2023", "L"), ("2026YTD", "L"), ("全周期", "L"),
               ("样本外2026YTD", "R"), ("滚动12月_202308", "R"),
               ("滚动12月_202502", "R"), ("滚动12月_202508", "R")],
    "688256": [("2024", "L"), ("2025", "L"), ("样本外2025", "R"),
               ("滚动12月_202302", "R"), ("滚动12月_202402", "R"),
               ("滚动12月_202408", "R")],
}
CASES = [(code, label, *(LONG_P if kind == "L" else ROLL_P)[label], "")
         for code, items in _SPEC.items() for label, kind in items]


def _snapshot() -> dict:
    """对 data/ 下所有情绪缓存取 sha256（写盘自证）。"""
    out = {}
    for p in sorted(DATA.glob("industry_*.csv")) + sorted(DATA.glob("index_*.csv")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _run_once(code: str, start: str, end: str, mode: str) -> dict:
    """mode='prod' 复现生产口径；mode='fixed' 用裁剪口径。"""
    real_loader = sentiment_data.load_industry_daily

    def prod_loader(ind_code, s, e):
        """复现 `sentiment_data.py:131-147`：命中缓存则返回**未裁剪全量**。"""
        target = DATA / f"industry_{ind_code.replace('.', '_')}_daily.csv"
        existing = pd.read_csv(target, parse_dates=["time"])
        b, e_dt = pd.Timestamp(s), pd.Timestamp(e)
        first, last = existing["time"].min(), existing["time"].max()
        in_range = ((existing["time"] >= b) &
                    (existing["time"] <= e_dt.replace(hour=15, minute=0))).sum()
        if (first <= b + pd.Timedelta(days=5)
                and last >= e_dt.replace(hour=15, minute=0) and in_range > 0):
            return existing                      # ← 生产：未裁剪
        raise RuntimeError("未命中缓存（本脚本不联网）")

    def fixed_loader(ind_code, s, e):
        """修正口径：裁剪到回测区间末（消除未来信息）。"""
        d = prod_loader(ind_code, s, e)
        return d[d["time"] <= pd.Timestamp(e)]

    sentiment_data.load_industry_daily = fixed_loader if mode == "fixed" else prod_loader
    try:
        df, source = core_data.load_data(code, start, end)
        strat = SentimentTStrategy()
        p = strat.default_params()
        # 仓位归一化（与 validate_strategies_long 同口径），使两次跑完全同参
        first_price = float(df["close"].iloc[0])
        shares = int((1_000_000.0 * 0.30 / first_price) // 100 * 100)
        p["base_position"] = shares
        r = strat.run(df, 1_000_000.0, p, {"code": code})
        return {
            "source": source,
            "bars": len(df),
            "days": pd.to_datetime(df["time"]).dt.date.nunique(),
            "return_pct": round(r.total_return * 100, 4),
            "trades": len(r.trades),
            "mdd_pct": round(r.max_drawdown * 100, 4),
            "final_equity": round(float(r.equity["equity"].iloc[-1]), 4) if hasattr(r, "equity") and len(r.equity) else None,
        }
    finally:
        sentiment_data.load_industry_daily = real_loader


def main() -> None:
    before = _snapshot()
    print("=" * 100)
    print("H32 端到端实测：只改 `load_industry_daily` 的裁剪口径，看回测数字是否改变")
    print("=" * 100)
    print(f"跑前缓存快照 {len(before)} 个文件\n")

    results = []
    for code, label, start, end, direction in CASES:
        print("-" * 100)
        print(f"【{code} / {label}】{start} ~ {end}")
        a = _run_once(code, start, end, "prod")
        b = _run_once(code, start, end, "fixed")
        d_ret = b["return_pct"] - a["return_pct"]
        d_trd = b["trades"] - a["trades"]
        verdict = "★改变" if (abs(d_ret) > 1e-6 or d_trd != 0) else "无变化"
        print(f"  生产(全量) 收益 {a['return_pct']:>10.4f}%  笔数 {a['trades']:>5}  回撤 {a['mdd_pct']:>9.4f}%")
        print(f"  修正(裁剪) 收益 {b['return_pct']:>10.4f}%  笔数 {b['trades']:>5}  回撤 {b['mdd_pct']:>9.4f}%")
        print(f"  Δ(修正−生产)      {d_ret:>+10.4f}%        {d_trd:>+5}         {b['mdd_pct']-a['mdd_pct']:>+9.4f}%"
              f"   ⇒ {verdict}")
        results.append((code, label, a, b, d_ret, d_trd, verdict))

    after = _snapshot()
    print()
    print("=" * 100)
    print("写盘自证（H31 机理：调一次就写盘 ⇒ 必须核对 sha256 未变）")
    print("=" * 100)
    changed = [k for k in before if before[k] != after.get(k)]
    if changed:
        print("  ❌ 以下文件被改写（脚本不该动盘）：")
        for c in changed:
            print(f"     {c}")
    else:
        print(f"  ✅ 全部 {len(before)} 个缓存文件 sha256 **未变** ⇒ 本次实测零写盘")

    print()
    print("=" * 100)
    print("汇总（全部 28 例「真影响」组合）")
    print("=" * 100)
    print(f"{'标的':<8}{'窗口':<18}{'生产收益%':>11}{'修正收益%':>11}"
          f"{'Δ收益%':>10}{'Δ笔数':>8}{'  '}")
    for code, label, a, b, d_ret, d_trd, verdict in results:
        print(f"{code:<8}{label:<18}{a['return_pct']:>11.4f}"
              f"{b['return_pct']:>11.4f}{d_ret:>+10.4f}{d_trd:>+8}  {verdict}")
    real = [r for r in results if "★" in r[6]]
    n = len(results)
    print()
    print(f"  ★ 数字改变 : {len(real)} / {n}  ({len(real)/n*100:.1f}%)")
    if real:
        ds = [r[4] for r in real]
        dts = [r[5] for r in real]
        print(f"  Δ收益% 范围 : {min(ds):+.4f} ~ {max(ds):+.4f}   "
              f"中位 {sorted(ds)[len(ds)//2]:+.4f}   均值 {sum(ds)/len(ds):+.4f}")
        print(f"  Δ笔数 范围 : {min(dts):+d} ~ {max(dts):+d}   均值 {sum(dts)/len(dts):+.1f}")
        # 方向性：穿越是系统性偏乐观还是随机？
        pos = sum(1 for d in ds if d < 0)   # 修正后变低 = 生产口径偏高
        print(f"  方向性     : 修正后收益**变低** {pos} 例 / 变高 {len(ds)-pos} 例"
              f"  ⇒ {'系统性偏乐观' if pos > len(ds)*0.7 else '方向不一致（随机噪声）' if pos < len(ds)*0.7 else '倾向偏乐观'}")


if __name__ == "__main__":
    main()
