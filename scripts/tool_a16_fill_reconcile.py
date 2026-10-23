#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A16 层②撮合 + 层③费用 对账工具 · **只读**

定位
----
`A16实盘对账_设计方案.md` §3.2 / §3.3 的执行面。
首单实盘成交后，用本工具做两层对账：
  层② 撮合：实测滑点分布 vs 假设 5 bps（**只记录，n 小不下结论**）
  层③ 费用：修正后三项公式重算 vs 实盘打包 commission（总额口径），
            并用实盘值反推「免规费」解读 A / B（§3.3 步骤 5）

红线（本脚本刻意不逾越）
------------------------
- **只读**：不 import `xttrader`、不触碰账户与下单接口、不写 `data/`。
- **不改生产参数**：修正后费率写死在本工具内（与 `paper_broker.py` 现值无关），
  回写生产参数属方案 §四-⑥，须走 mutation-validation，另行立项。
- **措辞红线**：即便总额对上，结论也只能写「三项之和与实盘一致」，
  **不得写「费用口径已验证」**（xttrader 只给打包值，三项可能两错相消，方案 §1.3）。

输入（实盘成交，首单后人工导出为 JSON）
------------------------------------
  [
    {"time": "2026-10-27 09:31:05",     # 北京时间，成交时刻
     "side": "buy",                      # buy / sell
     "price": 901.20,                    # 成交均价 XtTrade.traded_price
     "volume": 100,                      # 成交数量（股）traded_volume
     "commission": 8.30,                 # 打包费用 XtTrade.commission
     "traded_amount": 90120.0}           # 成交金额 traded_amount（可选，缺省按 price*volume）
  ]

用法
----
  D:\\Anaconda3\\python.exe scripts/tool_a16_fill_reconcile.py \
      --fills <成交JSON> --date 2026-10-27 \
      --local-csv data/live/stock_300308_1m.csv

  # 公式自检（不依赖任何实盘数据 / 行情文件）
  D:\\Anaconda3\\python.exe scripts/tool_a16_fill_reconcile.py --selftest

参照基准口径
------------
- AD 本地 CSV 时间标签 = bar **开始**（北京）；成交时刻 T 落入 [label, label+1) ⇒ 用该 bar 的 close。
- 印花税按 2023-08-28 起卖出单边 0.0005；过户费按 2022-04-29 起双向 0.00001（方案 §2.1）。
- 解读 A：佣金 = max(金额×0.00008, 1.0)；解读 B：A 的佣金 + 规费 金额×0.0000541（经手 0.0341‰ + 证管 0.02‰，方案 §2.4）。

产物
----
  test_outputs/a16_fill_reconcile/a16_{code}_{date}.json   机读结论
  test_outputs/a16_fill_reconcile/a16_{code}_{date}.csv    逐笔明细
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = APP_DIR / "test_outputs" / "a16_fill_reconcile"

# ── 修正后费率（A16 方案 §2.5 待改清单；与 paper_broker.py 现值刻意不同） ──
COMMISSION_RATE = 0.00008        # 万 0.8（用户 2026-09-27 确认）
MIN_COMMISSION = 1.0             # 最低 1 元
STAMP_TAX_RATE = 0.0005          # 印花税，卖出单边（2023-08-28 减半）
TRANSFER_FEE_RATE = 0.00001      # 过户费，双向（2022-04-29 下调）
REGFEE_RATE = 0.0000541          # 规费（仅解读 B）：经手 0.0341‰ + 证管 0.02‰
ASSUMED_SLIPPAGE_BPS = 5.0       # 项目假设滑点（paper_broker.py:31）

# 滑点判定门槛（方案 §3.2：n 小只记录不判定）
MIN_FILLS_FOR_SLIPPAGE_VERDICT = 10
# 费用单笔相对偏差判据（方案 §3.3：暂定 5%，须按实际分布再标定）
FEE_TOL = 0.05
# A/B 反推：相对距离小于该值才允许倾向某一解读
AB_TOL = 0.02


# ────────────────────────── 费用公式（纯函数，供 selftest） ──────────────────────────
def fees_A(amount: float, side: str) -> dict:
    """解读 A：万 0.8 = 全佣金（含规费），免规费。"""
    comm = max(amount * COMMISSION_RATE, MIN_COMMISSION)
    stamp = amount * STAMP_TAX_RATE if side == "sell" else 0.0
    transfer = amount * TRANSFER_FEE_RATE
    return {"commission": comm, "stamp_tax": stamp,
            "transfer_fee": transfer, "total": comm + stamp + transfer}


def fees_B(amount: float, side: str) -> dict:
    """解读 B：万 0.8 = 净佣金，规费另计（规费无最低）。"""
    a = fees_A(amount, side)
    regfee = amount * REGFEE_RATE
    comm_b = a["commission"] + regfee
    return {"commission": comm_b, "stamp_tax": a["stamp_tax"],
            "transfer_fee": a["transfer_fee"], "total": comm_b + a["stamp_tax"] + a["transfer_fee"]}


def slippage_bps(price: float, ref_close: float, side: str) -> float:
    """实测滑点（bp）。买：(价/参照−1)；卖：(1−价/参照)。方案 §3.2 设计 3。"""
    if ref_close <= 0:
        raise ValueError(f"参照 close 非法: {ref_close}")
    return ((price / ref_close - 1.0) if side == "buy" else (1.0 - price / ref_close)) * 1e4


def infer_ab(actual_total: float, amount: float, side: str) -> dict:
    """用实盘打包费用反推解读 A/B（方案 §3.3 步骤 5）。"""
    ta, tb = fees_A(amount, side)["total"], fees_B(amount, side)["total"]
    dA = abs(ta - actual_total) / actual_total if actual_total else float("inf")
    dB = abs(tb - actual_total) / actual_total if actual_total else float("inf")
    if dA < AB_TOL and dA < dB:
        verdict, basis = "A", f"|实盘−A|/实盘={dA:.4%} < |实盘−B|/实盘={dB:.4%}"
    elif dB < AB_TOL and dB < dA:
        verdict, basis = "B", f"|实盘−B|/实盘={dB:.4%} < |实盘−A|/实盘={dA:.4%}"
    elif dA < AB_TOL and dB < AB_TOL:
        verdict, basis = "不可判", f"A/B 距离均在 {AB_TOL:.0%} 内（A={dA:.4%}, B={dB:.4%}）"
    else:
        verdict, basis = "均不匹配", f"A={dA:.4%}, B={dB:.4%}，均 ≥ {AB_TOL:.0%}，疑口径外费用"
    return {"verdict": verdict, "dist_A": dA, "dist_B": dB, "basis": basis,
            "total_A": ta, "total_B": tb}


# ────────────────────────── 自检（不依赖外部数据） ──────────────────────────
def selftest() -> int:
    """用合成数据验证全部公式与推断逻辑；任何断言失败即 exit 1。"""
    checks: list[tuple[str, bool, str]] = []

    # 1) 费用 A：100 股 × 895.86 = 89,586；佣金 = 7.16688；卖：+ 印花 44.793 + 过户 0.89586
    amt = 100 * 895.86
    fa = fees_A(amt, "sell")
    checks += [
        ("A佣金=万0.8", abs(fa["commission"] - 7.16688) < 1e-9, str(fa["commission"])),
        ("A印花(卖)=0.0005", abs(fa["stamp_tax"] - 44.793) < 1e-9, str(fa["stamp_tax"])),
        ("A印花(买)=0", fees_A(amt, "buy")["stamp_tax"] == 0.0, ""),
        ("A过户=0.00001", abs(fa["transfer_fee"] - 0.89586) < 1e-9, str(fa["transfer_fee"])),
        ("A合计", abs(fa["total"] - (7.16688 + 44.793 + 0.89586)) < 1e-9, str(fa["total"])),
    ]
    # 2) 最低 1 元触发（低价小额）：100 股 × 100 元 = 10,000 → 万0.8 = 0.8 < 1 ⇒ 1.0
    checks.append(("最低1元触发", fees_A(10_000.0, "buy")["commission"] == 1.0,
                   str(fees_A(10_000.0, "buy")["commission"])))
    # 3) 解读 B：佣金 = A佣金 + 规费
    fb = fees_B(amt, "sell")
    checks.append(("B佣金=A佣金+规费", abs(fb["commission"] - (7.16688 + amt * REGFEE_RATE)) < 1e-9,
                   str(fb["commission"])))
    # 4) 滑点：买 901.20 vs 参照 900.00 ⇒ +13.333bp；卖 898.00 vs 900.00 ⇒ +22.222bp
    checks += [
        ("滑点买", abs(slippage_bps(901.20, 900.0, "buy") - 13.333333) < 1e-3,
         str(slippage_bps(901.20, 900.0, "buy"))),
        ("滑点卖", abs(slippage_bps(898.00, 900.0, "sell") - 22.222222) < 1e-3,
         str(slippage_bps(898.00, 900.0, "sell"))),
    ]
    # 5) A/B 反推两个方向 + 边界
    r_a = infer_ab(fa["total"], amt, "sell")
    r_b = infer_ab(fb["total"], amt, "sell")
    checks += [
        ("反推→A", r_a["verdict"] == "A", r_a["basis"]),
        ("反推→B", r_b["verdict"] == "B", r_b["basis"]),
        # A/B 差距恒 ≈ 规费/总额 ≈ 9.3%（卖出），中点距两者各 ≈4.7% ⇒ 均超 2% ⇒ 均不匹配。
        # （"不可判"分支在真实费率结构下防御性保留，正常不可达。）
        ("A/B中点→均不匹配", infer_ab((fa["total"] + fb["total"]) / 2, amt, "sell")["verdict"] == "均不匹配", ""),
        ("口径外→均不匹配", infer_ab(fa["total"] * 1.5, amt, "sell")["verdict"] == "均不匹配", ""),
    ]

    failed = [(n, d) for n, ok, d in checks if not ok]
    for n, ok, d in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
    print(f"selftest: {len(checks) - len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


# ────────────────────────── 主流程 ──────────────────────────
def load_bars(csv_path: Path, date: str) -> dict:
    """读 AD 本地 CSV（utf-8-sig 兼容 BOM），返回 {北京时间 'HH:MM': close}（限 date）。"""
    bars: dict[str, float] = {}
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            t = (row.get("time") or "").strip()
            if not t.startswith(date):
                continue
            hhmm = t[11:16]
            try:
                bars[hhmm] = float(row["close"])
            except (KeyError, ValueError):
                continue
    return bars


def ref_close_for(bars: dict[str, float], trade_time: str) -> tuple[str, float | None]:
    """成交时刻（北京）→ 所在分钟 bar 标签（[label, label+1)）→ 该 bar close。"""
    dt = datetime.strptime(trade_time, "%Y-%m-%d %H:%M:%S")
    label = dt.strftime("%H:%M")
    return label, bars.get(label)


def main() -> int:
    ap = argparse.ArgumentParser(description="A16 层②撮合 + 层③费用 对账（只读）")
    ap.add_argument("--fills", help="实盘成交 JSON 路径")
    ap.add_argument("--date", help="交易日 YYYY-MM-DD")
    ap.add_argument("--code", default="300308")
    ap.add_argument("--local-csv", default=str(APP_DIR / "data" / "live" / f"stock_300308_1m.csv"),
                    help="AD 本地 CSV（默认 data/live/，防陈旧须显式核对）")
    ap.add_argument("--selftest", action="store_true", help="公式自检后退出")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fails: list[str] = []

    # ── 前置自检（缺一 exit 2，零产物）──
    if not args.fills or not args.date:
        print("⛔ 缺 --fills 或 --date"); return 2
    fills_path = Path(args.fills)
    if not fills_path.is_file():
        print(f"⛔ 成交文件不存在: {fills_path}"); return 2
    csv_path = Path(args.local_csv)
    if not csv_path.is_file():
        print(f"⛔ 行情 CSV 不存在: {csv_path}"); return 2
    try:
        fills = json.loads(fills_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"⛔ 成交 JSON 解析失败: {exc}"); return 2
    if not isinstance(fills, list) or not fills:
        print("⛔ 成交列表为空或格式非法"); return 2
    bars = load_bars(csv_path, args.date)
    if not bars:
        print(f"⛔ CSV 中无 {args.date} 数据（核对 --local-csv 是否陈旧）"); return 2

    # ── 逐笔对账 ──
    rows, ab_votes, fee_ok = [], [], 0
    for i, fl in enumerate(fills):
        try:
            side = str(fl["side"]).lower()
            price, vol = float(fl["price"]), int(fl["volume"])
            amount = float(fl.get("traded_amount") or price * vol)
            actual_fee = float(fl["commission"]) if fl.get("commission") is not None else None
            label, ref = ref_close_for(bars, str(fl["time"]))
        except Exception as exc:
            fails.append(f"第 {i + 1} 笔字段非法: {exc}")
            continue
        if side not in ("buy", "sell"):
            fails.append(f"第 {i + 1} 笔 side 非法: {side}")
            continue
        if ref is None:
            fails.append(f"第 {i + 1} 笔无同分钟参照 bar（label={label}）——核对成交时刻与 CSV 覆盖")
            continue

        slip = slippage_bps(price, ref, side)
        fa, fb = fees_A(amount, side), fees_B(amount, side)
        ab = infer_ab(actual_fee, amount, side) if actual_fee else {"verdict": "无实盘费用", "dist_A": None,
                                                                    "dist_B": None, "basis": "",
                                                                    "total_A": fa["total"], "total_B": fb["total"]}
        rel_dev = (abs(fa["total"] - actual_fee) / actual_fee) if actual_fee else None
        if rel_dev is not None and rel_dev < FEE_TOL:
            fee_ok += 1
        if ab["verdict"] in ("A", "B"):
            ab_votes.append(ab["verdict"])
        rows.append({
            "time": fl["time"], "bar_label": label, "side": side, "price": price,
            "volume": vol, "amount": amount, "ref_close": ref,
            "slippage_bps": round(slip, 3), "vs_assumed_5bps": round(slip - ASSUMED_SLIPPAGE_BPS, 3),
            "fee_A_total": round(fa["total"], 4), "fee_B_total": round(fb["total"], 4),
            "actual_fee": actual_fee, "fee_rel_dev": None if rel_dev is None else round(rel_dev, 6),
            "min_comm_triggered": fa["commission"] == MIN_COMMISSION,
            "ab_verdict": ab["verdict"], "ab_basis": ab["basis"],
        })

    if fails:
        print("⛔ 数据问题（零产物退出）:")
        for x in fails:
            print("  -", x)
        return 2

    # ── 滑点统计（n 小只记录） ──
    slips = sorted(r["slippage_bps"] for r in rows)
    n = len(slips)
    med = slips[n // 2] if n % 2 else (slips[n // 2 - 1] + slips[n // 2]) / 2

    def pct(p: float) -> float:
        if n == 1:
            return slips[0]
        k = (n - 1) * p
        lo, hi = int(k), min(int(k) + 1, n - 1)
        return slips[lo] + (slips[hi] - slips[lo]) * (k - lo)

    ab_final = ("不可判" if len(set(ab_votes)) > 1 or not ab_votes
                else f"{ab_votes[0]}（{ab_votes.count(ab_votes[0])}/{len(ab_votes)} 笔一致）")

    verdict = {
        "meta": {"code": args.code, "date": args.date, "n_fills": n,
                 "csv": str(csv_path), "tool": "tool_a16_fill_reconcile.py",
                 "rates": {"commission": COMMISSION_RATE, "min_commission": MIN_COMMISSION,
                           "stamp_sell": STAMP_TAX_RATE, "transfer": TRANSFER_FEE_RATE,
                           "regfee_B_only": REGFEE_RATE, "assumed_slippage_bps": ASSUMED_SLIPPAGE_BPS}},
        "layer2_matching": {
            "slippage_bps": {"min": slips[0], "p25": round(pct(0.25), 3), "median": round(med, 3),
                             "p75": round(pct(0.75), 3), "max": slips[-1]},
            "verdict": ("记录" if n < MIN_FILLS_FOR_SLIPPAGE_VERDICT else
                        ("偏乐观，须评估上调" if med > ASSUMED_SLIPPAGE_BPS * 2 else
                         ("≈假设，可用" if med <= ASSUMED_SLIPPAGE_BPS * 1.5 else "观察"))),
            "verdict_note": f"n={n} < {MIN_FILLS_FOR_SLIPPAGE_VERDICT} ⇒ 只记录不判定（方案 §3.2 样本量纪律）"
                            if n < MIN_FILLS_FOR_SLIPPAGE_VERDICT else f"n={n}，达到判定样本下限",
            "participation_note": "1 手规模下 max_volume_participation=0.10 永不触发（方案 §3.2 已预记），本工具不输出该项",
        },
        "layer3_fees": {
            "fee_ok": f"{fee_ok}/{n}",
            "tolerance": FEE_TOL,
            "措辞红线": "结论只能写「修正后三项之和与实盘一致」，不得写「费用口径已验证」（方案 §1.3 两错相消风险）",
            "ab_inference": ab_final,
        },
    }

    # ── 产物 ──
    jpath = OUT_DIR / f"a16_{args.code}_{args.date}.json"
    cpath = OUT_DIR / f"a16_{args.code}_{args.date}.csv"
    jpath.write_text(json.dumps({"verdict": verdict, "fills": rows}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    with open(cpath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"✅ {n} 笔成交对账完成（{args.date}）")
    print(f"  层② 滑点: 中位 {med:.1f}bp（区间 {slips[0]:.1f} ~ {slips[-1]:.1f}）"
          f" → {verdict['layer2_matching']['verdict']}")
    print(f"  层③ 费用: {fee_ok}/{n} 笔偏差 < {FEE_TOL:.0%}；解读反推: {ab_final}")
    print(f"  产物: {jpath}")
    print(f"        {cpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
