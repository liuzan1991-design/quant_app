# -*- coding: utf-8 -*-
"""一次性加固：把 2026-11-01 ~ 2026-12-31 的交易日补进本地日历缓存。

背景：2026-11-14 国盛将关闭 miniQMT（含 xtdata），此后 `_fetch_year` 拉不到 QMT 日历，
缓存停在最后一个已知交易日，之后一律降级为"周一~五 + WARN"。11–12 月无法定节假日，
可确定性推出（全部周一~五为交易日），先补上，避免降级链在无告警的情况下空转。

只追加、不改动已有行；写入格式与 `_save_cache` 一致（utf-8-sig + csv 默认换行）。
"""
import csv
import io
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # scripts/ 的上一级 = 项目根
P = ROOT / "data" / "trading_calendar.csv"

rows = {}
with io.open(P, encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        rows[date.fromisoformat(r["date"])] = r["is_trading"] == "1"

before = len(rows)
last_before = max(rows) if rows else None

YEAR, MONTH = 2026, 11        # 需要补的起始月（2027 年等国务院公告后改这里）
d = date(YEAR, MONTH, 1)
end = date(YEAR, 12, 31)
added = 0
while d <= end:
    if d not in rows:
        rows[d] = d.weekday() < 5
        added += 1
    d += timedelta(days=1)

tmp = P.with_name(P.name + ".tmp")
with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["date", "is_trading"])
    for k in sorted(rows):
        w.writerow([k.isoformat(), "1" if rows[k] else "0"])
tmp.replace(P)

print(f"补前 {before} 条（末 {last_before}）→ 补后 {len(rows)} 条，新增 {added} 条")

# 逐项核验
import importlib
sys.path.insert(0, str(ROOT))
import core.trading_calendar as tc  # noqa: E402

importlib.reload(tc)
checks = [("2026-11-02", True), ("2026-11-07", False), ("2026-11-13", True),
          ("2026-11-14", False), ("2026-11-16", True), ("2026-12-25", True),
          ("2026-12-31", True), ("2027-01-01", None)]
for s, expect in checks:
    y, m, dd = (int(x) for x in s.split("-"))
    got = tc.is_trading_day(date(y, m, dd))
    mark = "✅" if (expect is None or got == expect) else "❌"
    print(f"  {mark} {s} → {got}" + ("" if expect is None else f"（期望 {expect}）"))
