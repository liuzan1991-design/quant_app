# -*- coding: utf-8 -*-
"""底仓集中度监控（H46-③ 第一步：只监控与告警，**不动交易行为**）。

为什么阈值是 10%，不是 12% 也不是 7.56%
-------------------------------------
- `RiskConfig.max_symbol_position_pct = 0.10`（live 的 `paper_state.json.risk_config` 实测值）
  已经是**单股上限**，只是它只拦**买入**（`check_order` 里比 projected 仓位），挡不住"价格上涨造成的被动漂移"。
- ⇒ 规则应与它**同源**：漂移超过 10% ⇒ 减回 10%。
- ❌ 12%（初版文档）＝比既有上限还宽，等于放行超限；
- ❌ 7.56%（"实际×1.2"）＝仓位还没到 10% 目标就减仓，方向反了。
  实测：现价 612.63、底仓 100 股、现金 916,207 ⇒ 7.56% 只需股价 749（+22%），
  10% 需 1,018（+66%），12% 需 1,249（+104%）。

本模块**只算不卖**：输出比值、是否超限、超限金额、触发价。是否自动减仓是独立决定
（自动减需改 `base_position` 语义，否则 `sellable = total - base_position` 恒 0、T 仓冻结）。
"""
from __future__ import annotations

DEFAULT_CAP = 0.10       # 与 RiskConfig.max_symbol_position_pct 同源
CAP_KIND = "position_cap"


def evaluate(position_value: float | None, equity: float | None,
             cap: float = DEFAULT_CAP, shares: int | None = None,
             cash: float | None = None) -> dict:
    """`position_value / equity` 与上限比较。

    Returns:
        dict(ratio, cap, exceeded, excess_value, price_for_cap, status)
        status ∈ {ok, exceeded, unknown}；unknown 表示拿不到持仓/权益（不当作合规）。
    """
    if (position_value is None or equity is None or equity <= 0
            or position_value < 0):
        return {"ratio": None, "cap": cap, "exceeded": False, "excess_value": None,
                "price_for_cap": None, "status": "unknown"}
    ratio = float(position_value) / float(equity)
    exceeded = ratio > cap
    excess = max(0.0, float(position_value) - cap * float(equity))
    price_for_cap = None
    if shares and shares > 0 and cash is not None and 0 < cap < 1:
        # 解：shares×p = cap×(cash + shares×p)
        price_for_cap = cap * float(cash) / (float(shares) * (1 - cap))
    return {"ratio": round(ratio, 6), "cap": cap, "exceeded": exceeded,
            "excess_value": round(excess, 2), "price_for_cap": (
                round(price_for_cap, 2) if price_for_cap else None),
            "status": "exceeded" if exceeded else "ok"}


# ── 告警分级（用户 2026-10-24 拍板）──────────────────────────────────────────
# 超限 0~1pp → info（只进状态文件与流水，不推桌面）；>1pp → alert（三通道全推）。
# 理由：集中度是**慢变量**，用告警级推送会挤占真故障的注意力。
ALERT_EXCESS_PP = 1.0


def grade(ratio: float | None, cap: float = DEFAULT_CAP) -> dict:
    """给定比值与上限，返回 (是否超限, 超限百分点, 告警级别)。"""
    if ratio is None:
        return {"exceeded": False, "excess_pp": None, "level": "info"}
    excess_pp = (float(ratio) - cap) * 100.0
    if excess_pp <= 0:
        return {"exceeded": False, "excess_pp": round(excess_pp, 2), "level": "info"}
    return {"exceeded": True, "excess_pp": round(excess_pp, 2),
            "level": "info" if excess_pp <= ALERT_EXCESS_PP else "alert"}
