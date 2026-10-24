#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""H46-③ 观察期进度：3a 只监控，何时该回到 3b 决策。

用法：D:\\Anaconda3\\python.exe scripts/tool_cap_watch.py
判据（2026-10-24 拍板，先到者为准）：
  · 累计 ≥3 个超限日 ⇒ decision（评估 3b）
  · 满 10 个交易日：零超限 ⇒ close_3b（关闭"自动减仓"议题，3a 永久留作水位计）
  · 其余 ⇒ accumulating
"""
from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from core.cap_watch import collect, evaluate_watch  # noqa: E402


def main() -> int:
    records = collect(APP / "live_outputs")
    got = evaluate_watch(records)
    print(f"观察进度：{got['days_observed']}/{got['days_required']} 交易日"
          f"（剩 {got['remaining_days']}），超限 {got['incidents']}/{got['incidents_required']} 次")
    print(f"状态：{got['status']} —— {got['note']}")
    for r in records[-12:]:
        flag = "⚠" if r.get("exceeded") else " "
        ratio = r.get("ratio")
        print(f"  {flag} {r.get('date')}  {ratio:.2%}" if ratio is not None else f"  {r}")
    if not records:
        print("（暂无 position_cap 记录：该字段自 2026-10-26 起随每日摘要写入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
