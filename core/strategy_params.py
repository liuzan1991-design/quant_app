# -*- coding: utf-8 -*-
"""离线/生产共用的策略参数规范化；保留输入分档，反复调用幂等。"""
from __future__ import annotations


def normalize_t_params(params):
    p = dict(params)
    old = p.get("stop_loss_pct", {})
    defaults = {"low": .013, "normal": .018, "high": .025}
    p["stop_loss_pct"] = {
        level: float(p.get(f"stop_loss_{level}", old.get(level, fallback)
                          if isinstance(old, dict) else old))
        for level, fallback in defaults.items()}
    if any(not 0 < value < 1 for value in p["stop_loss_pct"].values()):
        raise ValueError("止损比例必须在(0,1)内")
    return p
