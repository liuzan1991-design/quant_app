# -*- coding: utf-8 -*-
"""收盘/盘中巡检：四个纸面策略的当日成绩汇总（只读）。"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # scripts/ 的上一级 = 项目根
BASE = ROOT / "live_outputs" / "301717.SZ"
SIDS = ["grid_trade", "ma_swing", "intraday_t", "sentiment_t"]

print(f"{'策略':<14}{'初始':>10}{'昨收权益':>12}{'当前权益':>12}{'当日':>10}{'累计':>10}  持仓")
tot_day = 0.0
for sid in SIDS:
    p = BASE / sid / "paper_state.json"
    if not p.exists():
        print(f"{sid:<14}  (无 paper_state.json)")
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    acct = d.get("account") or d
    init = float(acct.get("initial_cash") or 0)
    cash = float(acct.get("cash") or 0)
    pos = acct.get("positions") or {}
    mv = 0.0
    holds = []
    for sym, v in pos.items():
        q = int(v.get("quantity") or 0)
        if q:
            px = float(v.get("market_price") or 0)
            mv += q * px
            holds.append(f"{sym}×{q}@{px:.2f}(成本{v.get('average_cost', 0):.2f})")
    eq = cash + mv
    w = BASE / sid / "watermark.json"
    wd = json.loads(w.read_text(encoding="utf-8")) if w.exists() else {}
    dse = float(wd.get("day_start_equity") or 0)
    anchor = wd.get("day_anchor_date") or "-"
    day = eq - dse if dse else 0.0
    tot_day += day
    print(f"{sid:<14}{init:>10,.0f}{dse:>12,.2f}{eq:>12,.2f}{day:>+10,.2f}"
          f"{eq - init:>+10,.0f}  {'; '.join(holds) if holds else '空仓'}")
print(f"{'合计当日':<14}{'':>10}{'':>12}{'':>12}{tot_day:>+10,.2f}")
