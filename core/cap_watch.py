# -*- coding: utf-8 -*-
"""H46-③ 观察期退出条件（用户 2026-10-24 拍板的**预设决策门**）。

背景：3a 只监控不动作。若不预设退出条件，"攒几天数据"会无限期悬着——
**决策门必须在动手前就定好，否则等于没有决策**。

判据（先到者为准）
----------------
1. **累计出现 ≥3 个超限日** ⇒ `decision`（够 3 次样本，评估 3b）；
2. **满 10 个交易日**：
   - 零超限 ⇒ **`close_3b`**：直接关闭"自动减仓"议题，3a 永久保留当水位计；
   - 有 1~2 次超限 ⇒ `decision`（样本不足但窗口已到，仍需人工评估）；
3. 其余 ⇒ `accumulating`。

⚠️ 阈值是 **10%**（`RiskConfig.max_symbol_position_pct`），不是 7.56%/12%。
   当前 6.27% ⇒ 需股价涨到 1,018（+66%）才可能超限 ⇒ 10 日内大概率零超限。
"""
from __future__ import annotations

import json
from pathlib import Path

WATCH_DAYS = 10
WATCH_INCIDENTS = 3


def evaluate_watch(records: list[dict]) -> dict:
    """`records` 每项至少含 `{date, ratio, cap, exceeded}`（来自每日摘要 position_cap）。"""
    usable = [r for r in records if r.get("ratio") is not None]
    n = len(usable)
    incidents = sum(1 for r in usable if r.get("exceeded"))
    if incidents >= WATCH_INCIDENTS:
        status = "decision"
    elif n >= WATCH_DAYS:
        status = "close_3b" if incidents == 0 else "decision"
    else:
        status = "accumulating"
    return {
        "status": status,
        "days_observed": n,
        "days_required": WATCH_DAYS,
        "incidents": incidents,
        "incidents_required": WATCH_INCIDENTS,
        "remaining_days": max(0, WATCH_DAYS - n),
        "note": {
            "accumulating": "继续观察（3a 只记不动）",
            "decision": "到点，重新评估 3b（自动减仓）",
            "close_3b": "10 日零超限 ⇒ 关闭 3b 议题，3a 永久保留作水位计",
        }[status],
    }


def collect(root: Path) -> list[dict]:
    """从 `live_outputs/*/*/daily_summary_*.json` 提取 position_cap 记录（按日去重）。"""
    seen: dict[str, dict] = {}
    for p in sorted(Path(root).glob("*/*/daily_summary_*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            cap = d.get("position_cap")
            if isinstance(cap, dict) and cap.get("ratio") is not None:
                seen[str(d.get("date"))] = {"date": d.get("date"), **cap}
        except Exception:  # noqa: BLE001 —— 单文件坏不影响整体
            continue
    return [seen[k] for k in sorted(seen)]
