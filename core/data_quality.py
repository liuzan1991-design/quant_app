# -*- coding: utf-8 -*-
"""数据完整性判定（H35 缺口标记）。

问题
----
H35 的修复只做到"硬收盘兜底可达"，**还差一半**：D11 那天只采到 **161/240** 根，
摘要照样写出、看不出异常 ⇒ 外部读成"有摘要 = 数据完整"，进而把残缺日计入样本。

判据（来自 E3 / 登记原文）
------------------------
- 一分钟线每交易日应有 **240** 根（09:30–11:29 + 13:00–14:59）；
- **缺口比例 ≥ 5% ⇒ 该日作废**（`status="void"`），不得计入有效样本；
- 0 < 缺口 < 5% ⇒ `status="gap"`（可用但标记）；
- 满 240 ⇒ `status="complete"`；
- **取不到根数**（文件缺失/异常）⇒ `status="unknown"`，且 `usable_for_sample=False`
  —— 宁可标"不知道"也不假装完整。

本模块只做判定，不读文件；计数由调用方给（便于单测）。
"""
from __future__ import annotations

EXPECTED_1M_BARS = 240        # 09:30–11:29 + 13:00–14:59
GAP_VOID_THRESHOLD = 0.05     # E3 判据：缺口 ≥5% 该日作废


def day_bar_stats(bars: int | None, expected: int = EXPECTED_1M_BARS,
                  void_threshold: float = GAP_VOID_THRESHOLD) -> dict:
    """按当日 bar 数给出完整性判定。

    Args:
        bars: 当日实际 bar 数；None 表示**无法确定**（不是"0 根"）。
        expected: 应有根数，默认 240。
        void_threshold: 作废阈值（缺口比例），默认 5%。

    Returns:
        dict(bars, expected, gap_pct, status, usable_for_sample)
        status ∈ {complete, gap, void, unknown}
    """
    if bars is None:
        return {"bars": None, "expected": int(expected), "gap_pct": None,
                "status": "unknown", "usable_for_sample": False}
    bars = int(bars)
    gap = max(0.0, 1.0 - bars / float(expected)) if expected else 0.0
    if bars >= expected:
        status = "complete"
    elif gap >= void_threshold:
        status = "void"
    else:
        status = "gap"
    return {"bars": bars, "expected": int(expected), "gap_pct": round(gap, 6),
            "status": status,
            "usable_for_sample": bars >= expected or gap < void_threshold}
