# -*- coding: utf-8 -*-
"""只读验证：按当前持仓 + cap 25%，明天开盘各策略还能不能买。"""
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # scripts/ 的上一级 = 项目根
sys.path.insert(0, str(ROOT))

from core import live_config  # noqa: E402
from core.cross_risk import CrossSymbolLedger  # noqa: E402

BASE = ROOT / "live_outputs" / "301717.SZ"
SYM = "301717.SZ"

cfg = live_config.load()
cap = cfg.cross_symbol_cap
base_cash = cfg.initial_cash

holds = {}
price = None
for sid in ("grid_trade", "ma_swing", "intraday_t", "sentiment_t"):
    p = BASE / sid / "paper_state.json"
    if not p.exists():
        holds[sid] = 0
        continue
    d = json.load(io.open(p, encoding="utf-8"))
    pos = ((d.get("account") or {}).get("positions") or {}).get(SYM) or {}
    qty = int(pos.get("quantity") or 0)
    holds[sid] = qty
    px = pos.get("market_price")
    if px:
        price = float(px)

if price is None:
    price = 279.40      # 兜底：今日收盘附近

ledger = CrossSymbolLedger(cap, base_cash)
for sid, q in holds.items():
    ledger.update(sid, SYM, q)

total_shares = ledger.total(SYM)
total_value = total_shares * price
print(f"cap = {cap:.0%} | 额度 = {ledger.cap_value():,.0f} 元")
print(f"当前合计持仓 {total_shares} 股 × {price:.2f} = {total_value:,.0f} 元"
      f"（占额度 {total_value / ledger.cap_value():.0%}）")
print("分策略：" + "，".join(f"{k}={v}" for k, v in holds.items()))
print()
for sid in holds:
    room = ledger.allowed_buy(SYM, price, sid, holds[sid])
    ok = ledger.check(SYM, 100, price, sid, holds[sid], sid)
    print(f"  {sid:14s} 可买额度 {room:>9.1f} 股（{room * price:>11,.0f} 元）  买100股 → {'放行' if ok else '拒绝'}")
    ledger.rejects.clear()

print()
print(f"结论：合计已超额度 {total_value - ledger.cap_value():,.0f} 元，"
      f"需减到 {int(ledger.cap_value() // price // 100) * 100} 股以内才恢复买入能力")
