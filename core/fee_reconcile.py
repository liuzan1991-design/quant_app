"""费用口径：柜台 commission 字段不可用时的替代取数路径。

实证（2026-10-28 真实账户，两次只读快照）
    12:57（`probe_20261028_075759.json`）cash = 397,098.39，orders/trades 均为空；
    13:50（`probe_20261028_085035.json`）cash = 469,754.41，orders = 7、trades = 11。
    期间 卖出 4 笔共 110,394.00、买入 3 笔共 37,670.00 ⇒ 净流入 72,724.00，
    实际增加 72,656.02 ⇒ **差额法得费用 67.98 元**。
    同一份 trades 里 `m_dCommission` **11/11 全为 0**（成交后约 50 分钟仍未回填）。

结论与边界
    1. `commission` 为 0 不等于"免佣金"，只等于"该字段此刻不可用"。不得据此
       宣称零佣金，也不得用 0 去做盈亏核算。
    2. 差额法可算出**费用总额**，且不依赖任何费用字段；但它只能算总额，
       不能拆项，也不能区分"佣金/印花税/过户费"。
    3. 差额法只在**同一交易日内**两次快照之间成立：跨日会混入 T+1 交收、
       利息、红利、手工转账等其他资金业务。跨日一律拒绝，不猜。
    4. 拆项（印花税/过户费/佣金）只能作为**估算**展示，须标注估计值；
       真实费率以券商对账单为准，不得据估算值写"费用口径已验证"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

BEIJING = timezone(timedelta(hours=8))

STAMP_DUTY_RATE = 0.0005      # 印花税：卖出单边 0.05%（2023-08-28 起减半后）
TRANSFER_FEE_RATE = 0.00001   # 过户费：双边 0.001%
CASH_TOL = 0.01               # 金额比对容差（元）

SIDES = ("BUY", "SELL")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch(value: Any) -> datetime | None:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), BEIJING)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=BEIJING)
    return None


def commission_usable(trades: Iterable[Mapping[str, Any]]) -> tuple[bool, str]:
    """判 `m_dCommission` 能否直接用于核算。

    有成交但佣金全为 0/缺失 ⇒ 不可用（实测形态）；任何一笔非 0 ⇒ 可用。
    """
    items = list(trades)
    if not items:
        return False, "无成交记录，无从判断"
    vals = [_num(t.get("m_dCommission", t.get("commission"))) for t in items]
    nonzero = [v for v in vals if v not in (None, 0.0)]
    if nonzero:
        return True, f"{len(nonzero)}/{len(items)} 笔佣金非 0，可直接取用"
    return False, (f"{len(items)} 笔成交的佣金字段全为 0/缺失 ⇒ 该字段不可用"
                   f"（可能是当日未回填，须 T+1 复采）")


def split_trades(trades: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """按方向汇总成交金额与股数。方向只认 m_nOffsetFlag（48 买 / 49 卖）。"""
    out = {"buy_amount": 0.0, "sell_amount": 0.0, "buy_qty": 0.0, "sell_qty": 0.0}
    for t in trades:
        flag = t.get("m_nOffsetFlag", t.get("offset_flag"))
        try:
            side = {48: "BUY", 49: "SELL"}.get(int(flag))
        except (TypeError, ValueError):
            side = None
        if side is None:
            continue
        qty = _num(t.get("m_nTradedVolume", t.get("traded_volume"))) or 0.0
        amount = _num(t.get("m_dTradedAmount", t.get("traded_amount")))
        if amount is None:
            price = _num(t.get("m_dTradedPrice", t.get("traded_price"))) or 0.0
            amount = qty * price
        if side == "BUY":
            out["buy_amount"] += amount
            out["buy_qty"] += qty
        else:
            out["sell_amount"] += amount
            out["sell_qty"] += qty
    return out


def trades_in_window(trades: Iterable[Mapping[str, Any]],
                     t0: datetime, t1: datetime) -> list[Mapping[str, Any]]:
    """取 [t0, t1] 内的成交（按 m_nTradedTime 北京时间）。"""
    lo, hi = min(t0, t1), max(t0, t1)
    out = []
    for t in trades:
        ts = _epoch(t.get("m_nTradedTime", t.get("traded_time")))
        if ts is None:
            continue
        if lo <= ts <= hi:
            out.append(t)
    return out


def fee_by_cash_delta(*, cash_before: float, cash_after: float,
                      buy_amount: float, sell_amount: float) -> dict[str, Any]:
    """差额法：费用 = (期初 + 卖出金额 - 买入金额) - 期末。"""
    net_flow = sell_amount - buy_amount
    expected_no_fee = cash_before + net_flow
    fee = expected_no_fee - cash_after
    return {
        "cash_before": cash_before, "cash_after": cash_after,
        "buy_amount": buy_amount, "sell_amount": sell_amount,
        "net_flow": net_flow, "expected_no_fee": expected_no_fee,
        "fee": fee, "ok": fee >= -CASH_TOL,
        "note": "" if fee >= -CASH_TOL else "费用为负 ⇒ 期间有未记录的资金业务（转入/利息/红利）",
    }


def decompose(fee_total: float, *, buy_amount: float, sell_amount: float,
              stamp_rate: float = STAMP_DUTY_RATE,
              transfer_rate: float = TRANSFER_FEE_RATE) -> dict[str, Any]:
    """把费用总额**估算**拆项。仅作参考，不得当已核验费率。"""
    stamp = sell_amount * stamp_rate
    transfer = (buy_amount + sell_amount) * transfer_rate
    commission = fee_total - stamp - transfer
    turnover = buy_amount + sell_amount
    return {
        "estimate": True,
        "stamp_duty": stamp, "transfer_fee": transfer, "commission": commission,
        "turnover": turnover,
        "commission_rate_pct": (commission / turnover * 100.0) if turnover else None,
        "note": ("拆项为估算值：印花税按卖出 0.05%、过户费按双边 0.001% 计，"
                 "剩余归佣金；真实费率以券商对账单为准，不得据此宣称已核验"),
    }


def resolve_from_snapshots(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """用两次柜台快照（`tool_qmt_trader_probe.py` 输出）定费用口径。

    优先用 commission 字段；不可用时退回差额法；跨日一律拒绝。
    """
    def _acc(snap: Mapping[str, Any]) -> Mapping[str, Any]:
        accs = snap.get("accounts") or []
        return accs[0] if accs else {}

    def _probe_time(snap: Mapping[str, Any]) -> datetime | None:
        raw = snap.get("probe_at")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
        # probe_at 是本机墙钟（本机 GMT+3）；naive 一律按系统本地时区解释再换北京，
        # 不硬编码时差（历史教训：硬编码 5 小时在换机器后必错）。
        return (dt if dt.tzinfo else dt.astimezone()).astimezone(BEIJING)

    a, b = _acc(before), _acc(after)
    cash_before = _num((a.get("asset") or {}).get("cash"))
    cash_after = _num((b.get("asset") or {}).get("cash"))
    trades_all = list(b.get("trades") or [])
    t0, t1 = _probe_time(before), _probe_time(after)

    out: dict[str, Any] = {"status": "insufficient_evidence", "reason": "",
                           "trades_total": len(trades_all)}
    if cash_before is None or cash_after is None:
        out["reason"] = "快照缺 asset.cash，无法核算"
        return out
    if t0 is None or t1 is None:
        out["reason"] = "快照缺 probe_at，无法界定成交窗口"
        return out
    if t0.date() != t1.date():
        out["reason"] = (f"两次快照跨交易日（{t0.date()} → {t1.date()}）；差额法会混入 "
                         f"T+1 交收与利息/红利，拒绝核算")
        out["cross_day"] = True
        return out

    window = trades_in_window(trades_all, t0, t1)
    sums = split_trades(window)
    usable, why = commission_usable(window)
    out.update({"cash_before": cash_before, "cash_after": cash_after,
                "window": [t0.strftime("%Y-%m-%dT%H:%M:%S"), t1.strftime("%Y-%m-%dT%H:%M:%S")],
                "trades_in_window": len(window), **sums})

    # 窗口内无成交、现金却变了 ⇒ 更可能是时间基准不一致（本机时区/快照时刻），
    # 而不是"期间有未记录资金业务"。两者必须分开说，否则会误导排查方向。
    if not window and abs(cash_after - cash_before) > CASH_TOL:
        out["reason"] = (f"窗口内 0 笔成交，但现金变动 {cash_after - cash_before:+.2f} ⇒ "
                         f"快照时间基准可能不一致（probe_at 时区/墙钟），或期间有转账/利息；"
                         f"先核时区再重算")
        out["clock_suspect"] = True
        return out

    delta = fee_by_cash_delta(cash_before=cash_before, cash_after=cash_after,
                              buy_amount=sums["buy_amount"], sell_amount=sums["sell_amount"])
    out["cash_delta"] = delta

    if usable:
        fee_sum = sum((_num(t.get("m_dCommission")) or 0.0) for t in window)
        out.update({"status": "commission_field", "fee": fee_sum, "source": "m_dCommission",
                    "reason": why})
        if abs(fee_sum - delta["fee"]) > CASH_TOL:
            out["warning"] = (f"commission 合计 {fee_sum:.2f} 与差额法 {delta['fee']:.2f} "
                              f"不一致 ⇒ 至少一方漏项，须人工核对")
        return out

    if not delta["ok"]:
        out["reason"] = delta["note"]
        return out
    out.update({"status": "cash_delta", "fee": delta["fee"], "source": "cash_delta",
                "reason": why,
                "decomposition": decompose(delta["fee"],
                                           buy_amount=sums["buy_amount"],
                                           sell_amount=sums["sell_amount"])})
    return out


def render(result: Mapping[str, Any]) -> str:
    """把结论排成可贴进日报的短表。"""
    lines = [f"## 费用口径核算（{result.get('status')}）", ""]
    if result.get("status") == "insufficient_evidence":
        lines.append(f"**证据不足**：{result.get('reason')}")
        return "\n".join(lines)
    lines += [
        "| 项 | 值 |", "|---|---|",
        f"| 时间窗（北京） | {result['window'][0]} → {result['window'][1]} |",
        f"| 窗口内成交 | {result['trades_in_window']} 笔 |",
        f"| 买入金额 | {result['buy_amount']:.2f} |",
        f"| 卖出金额 | {result['sell_amount']:.2f} |",
        f"| 现金 | {result['cash_before']:.2f} → {result['cash_after']:.2f} |",
        f"| 费用（{result['source']}） | **{result['fee']:.2f}** |",
    ]
    if result.get("decomposition"):
        d = result["decomposition"]
        lines += [
            "",
            "| 估算拆项 | 值 |", "|---|---|",
            f"| 印花税（卖出 0.05%） | {d['stamp_duty']:.2f} |",
            f"| 过户费（双边 0.001%） | {d['transfer_fee']:.2f} |",
            f"| 佣金（余额） | {d['commission']:.2f} |",
            f"| 佣金率 | {d['commission_rate_pct']:.4f}% |",
        ]
        lines += ["", f"> {d['note']}"]
    if result.get("warning"):
        lines += ["", f"⚠ {result['warning']}"]
    return "\n".join(lines)
