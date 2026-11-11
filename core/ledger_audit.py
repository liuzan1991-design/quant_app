"""日终账本比对：拟下单 ↔ 订单 ↔ 成交 ↔ 现金/持仓算术。

用途
    影子验收（阶段 1）的「拟下单与手工复算一致」这一条，需要一个能每天
    机械跑、不需要人肉按计算器的对照表。本模块只做离线核算，不写
    paper_state、不下单、不改任何生产配置。

为什么是「算术 + 行情事实」而不是「重跑策略」
    把策略阈值公式再写一遍 = 把同一份错误写两遍；两处一起错时比对表会
    说"一致"，那是最坏的假绿。因此这里复算的是三件与策略意图无关的事实：
      A 现金闭环   initial - Σ(买入金额+费用) + Σ(卖出金额-费用) == cash_end
      B 持仓闭环   Σ买入 - Σ卖出 == state.positions[symbol].quantity
      C 成交价落在同一分钟 bar 的 [low, high] 内
    A/B 抓台账漂移（H59 家族、幽灵成交 H77），C 抓撮合价错或用了不存在的
    行情（H17）。策略意图只作为「拟下单」列原样展示，不参与判定。

实测基线（2026-11-12 取数，用于设阈值，不是假设）
    - A/B：300319/grid_trade、300319/intraday_t、301717/grid_trade 三份
      state 全部 diff = 0.000000 ⇒ 当前数据零误报。
    - C：8 笔真实成交（301717 七笔、300319 一笔）**8/8 落在 bar 区间内**，
      最小余量 0.021%，最大 1.334%。故 C 用严格包含判定，不给容差；
      任何越界都值得人看一眼（宁可误报一次，也不要静默放过）。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

CASH_TOL = 0.01          # 现金比对容差（元）：1 分钱，四舍五入噪声以内
QTY_TOL = 0              # 持仓比对容差：股，整数不设容差
BAR_LOOKBACK_MIN = 2     # 成交时刻无 bar 时，最多回退 2 分钟找上一根


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_bars(path: str | Path) -> dict[str, dict[str, float]]:
    """读分钟 CSV（AD 导出）。键 = 'YYYY-MM-DDTHH:MM'，与 filled_at[:16] 对齐。

    兼容首行 BOM（'\\ufefftime'）与空格分隔的时间列；非日期行直接跳过。
    """
    bars: dict[str, dict[str, float]] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 5:
                continue
            key = (row[0] or "").strip()
            if len(key) < 16 or key[4] != "-":
                continue
            o, h, l, c = _num(row[1]), _num(row[2]), _num(row[3]), _num(row[4])
            if None in (o, h, l, c):
                continue
            bars[key.replace(" ", "T")[:16]] = {"open": o, "high": h, "low": l, "close": c}
    return bars


def find_bar(bars: Mapping[str, dict[str, float]], ts: str,
             lookback: int = BAR_LOOKBACK_MIN):
    """按成交时刻找 bar。返回 (bar, shift)；shift 为负数表示回退了几分钟。

    回退是为了兼容「末根标 14:59、无 11:30/15:00 标签」的既有事实，
    但回退必须被记录（shift != 0 本身就是要给人看的信号）。
    """
    key = (ts or "")[:16]
    if key in bars:
        return bars[key], 0
    try:
        dt = datetime.strptime(key, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None, None
    for k in range(1, lookback + 1):
        prev = (dt - timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M")
        if prev in bars:
            return bars[prev], -k
    return None, None


def check_fill_price(fill: Mapping[str, Any],
                     bars: Mapping[str, dict[str, float]]) -> dict[str, Any]:
    """成交价是否落在同分钟 bar 的 [low, high] 内（严格包含，无容差）。"""
    price = _num(fill.get("price"))
    ts = str(fill.get("filled_at") or "")
    bar, shift = find_bar(bars, ts)
    out = {"filled_at": ts, "price": price, "shift": shift,
           "inside": None, "margin_lo_pct": None, "margin_hi_pct": None,
           "bar": bar, "level": "ok", "note": ""}
    if price is None:
        out.update(level="blocker", note="成交价缺失")
        return out
    if bar is None:
        out.update(level="warn", note="该时刻无 bar（含回退）⇒ 需人工确认")
        return out
    lo, hi = bar["low"], bar["high"]
    out["inside"] = lo - 1e-9 <= price <= hi + 1e-9
    if lo > 0:
        out["margin_lo_pct"] = (price - lo) / lo * 100.0
    if hi > 0:
        out["margin_hi_pct"] = (price - hi) / hi * 100.0
    if not out["inside"]:
        out.update(level="blocker",
                   note=f"成交价 {price} 越出 bar [{lo}, {hi}]")
    elif shift:
        out["note"] = f"用上一根 bar（回退 {abs(shift)} 分钟）"
    return out


def recompute_cash(initial_cash: float, fills: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """按成交明细重算期末现金。费用单列，不摊进价格。"""
    buy = sell = fee = 0.0
    missing_fee = 0
    for f in fills:
        qty = _num(f.get("quantity")) or 0.0
        price = _num(f.get("price")) or 0.0
        amount = qty * price
        if _num(f.get("fee")) is None:
            missing_fee += 1
        fee += _num(f.get("fee")) or 0.0
        if str(f.get("side")).upper() == "BUY":
            buy += amount
        else:
            sell += amount
    return {"initial": float(initial_cash), "buy_amount": buy, "sell_amount": sell,
            "fee": fee, "expected": float(initial_cash) - buy + sell - fee,
            "missing_fee": missing_fee}


def recompute_position(fills: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """按成交明细重算各标的持仓（股）：买 +、卖 −。"""
    out: dict[str, int] = {}
    for f in fills:
        sym = str(f.get("symbol") or "")
        qty = int(_num(f.get("quantity")) or 0)
        out[sym] = out.get(sym, 0) + (qty if str(f.get("side")).upper() == "BUY" else -qty)
    return out


def _state_position_qty(positions: Mapping[str, Any] | None, symbol: str) -> int:
    if not positions:
        return 0
    node = positions.get(symbol) or {}
    if isinstance(node, Mapping):
        return int(_num(node.get("quantity")) or 0)
    return 0


def _day_of(value: Any) -> str:
    return str(value or "")[:10]


def filter_day(records: Iterable[Mapping[str, Any]], day: str,
               key: str) -> list[Mapping[str, Any]]:
    return [r for r in records if _day_of(r.get(key)) == day]


def audit_day(*, day: str, symbol: str,
              signals: Sequence[Mapping[str, Any]],
              orders: Sequence[Mapping[str, Any]],
              fills: Sequence[Mapping[str, Any]],
              state: Mapping[str, Any],
              bars: Mapping[str, dict[str, float]]) -> dict[str, Any]:
    """当日账本比对。orders/fills 传**全历史**（用于算术闭环），内部按日切分。

    blocks:  硬失败（现金/持仓不闭环、成交价越界、成交价缺失）
    warnings: 需人工确认但不阻断（无 bar、回退 bar、费用缺失、拟下单无对应订单）
    """
    account = (state or {}).get("account") or {}
    initial = _num(account.get("initial_cash")) or 0.0
    actual_cash = _num(account.get("cash"))
    positions = account.get("positions") or {}

    cash = recompute_cash(initial, fills)
    pos = recompute_position(fills)
    corp = account.get("applied_corporate_actions") or {}
    cash["actual"] = actual_cash
    cash["diff"] = None if actual_cash is None else actual_cash - cash["expected"]
    cash["ok"] = cash["diff"] is not None and abs(cash["diff"]) <= CASH_TOL
    # 账本记过公司行为（分红/除权/送转）时，现金差额可能由事件造成，不能一律判缺陷。
    # 但也不能自动放过：只降级为「须按事件核对」，并把差额原样报出来。
    cash["corporate_actions"] = dict(corp)
    cash["explained_by_corp"] = bool(corp) and not cash["ok"]

    expect_qty = pos.get(symbol, 0)
    actual_qty = _state_position_qty(positions, symbol)
    position = {"from_fills": expect_qty, "in_state": actual_qty,
                "diff": actual_qty - expect_qty,
                "ok": abs(actual_qty - expect_qty) <= QTY_TOL}

    day_fills = filter_day(fills, day, "filled_at")
    price_checks = [check_fill_price(f, bars) for f in day_fills]

    day_orders = [o for o in orders
                  if day in (_day_of((o.get("request") or {}).get("created_at")),
                             _day_of(o.get("updated_at")))]
    day_signals = filter_day(signals, day, "signal_time")

    order_ids = {str((o.get("request") or {}).get("client_order_id") or "") for o in day_orders}
    fill_by_order: dict[str, list[Mapping[str, Any]]] = {}
    for f in day_fills:
        fill_by_order.setdefault(str(f.get("client_order_id") or ""), []).append(f)

    # 拒单在 signals 里就落 status=REJECTED 且 client_order_id 为空，不进 orders 表；
    # 只有「声称已提交」的拟下单才该在订单表里出现，否则才是真断链。
    rejects = [s for s in day_signals
               if str(s.get("order_status") or "").upper() in {"REJECTED", "BLOCKED"}]
    orphan_signals = []
    for s in day_signals:
        if s in rejects:
            continue
        coid = str(s.get("client_order_id") or "")
        if not coid or coid not in order_ids:
            orphan_signals.append(s)
    qty_mismatch = []
    for o in day_orders:
        req = o.get("request") or {}
        coid = str(req.get("client_order_id") or "")
        filled = int(_num(o.get("filled_quantity")) or 0)
        from_fills = sum(int(_num(f.get("quantity")) or 0) for f in fill_by_order.get(coid, []))
        if filled != from_fills:
            qty_mismatch.append({"client_order_id": coid, "order": filled, "fills": from_fills})
    no_fill_orders = [str((o.get("request") or {}).get("client_order_id") or "")
                      for o in day_orders
                      if str(o.get("status") or "").upper() == "FILLED"
                      and not fill_by_order.get(str((o.get("request") or {}).get("client_order_id") or ""))]

    blockers: list[str] = []
    warnings: list[str] = []
    if not cash["ok"]:
        if cash["explained_by_corp"]:
            warnings.append(
                f"现金差 {cash['diff']:+.2f} 元，但账本记录了公司行为 {cash['corporate_actions']}"
                f" ⇒ 可能来自分红/除权，须按事件核对（不判阻断，也不等于已解释）")
        else:
            blockers.append(f"现金不闭环：重算 {cash['expected']:.2f} vs 账面 "
                            f"{actual_cash} ⇒ 差 {cash['diff']:+.2f}")
    if not position["ok"]:
        blockers.append(f"持仓不闭环：重算 {expect_qty} 股 vs 账面 {actual_qty} 股")
    for c in price_checks:
        if c["level"] == "blocker":
            blockers.append(f"{c['filled_at']} {c['note']}")
        elif c["level"] == "warn":
            warnings.append(f"{c['filled_at']} {c['note']}")
    if cash["missing_fee"]:
        warnings.append(f"{cash['missing_fee']} 笔成交缺 fee 字段（按 0 计）")
    if orphan_signals:
        warnings.append(f"{len(orphan_signals)} 条声称已提交的拟下单在订单表里找不到")
    if qty_mismatch:
        blockers.append(f"{len(qty_mismatch)} 笔订单的 filled_quantity 与成交明细不符")
    if no_fill_orders:
        warnings.append(f"{len(no_fill_orders)} 笔标 FILLED 但无成交明细")

    return {
        "day": day, "symbol": symbol,
        "counts": {"signals": len(day_signals), "orders": len(day_orders),
                   "fills": len(day_fills)},
        "cash": cash, "position": position,
        "price": {"checked": len(price_checks), "detail": price_checks},
        "orders": {
            "rejects": len(rejects),
            "orphan_signals": [str(s.get("client_order_id") or "") for s in orphan_signals],
            "qty_mismatch": qty_mismatch,
            "no_fill": no_fill_orders,
        },
        "blockers": blockers, "warnings": warnings,
        "verdict": "mismatch" if blockers else "ok",
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    """把比对结果排成可直接贴进日报的表。"""
    c = report["cash"]
    p = report["position"]
    n = report["counts"]
    if c["diff"] is None:
        cash_row = f"| 现金 | {c['expected']:.2f} | — | — | ❌ 账面缺失 |"
    else:
        if c["ok"]:
            mark = "✅"
        elif c.get("explained_by_corp"):
            mark = "⚠ 有公司行为，须核对"
        else:
            mark = "❌"
        cash_row = (f"| 现金 | {c['expected']:.2f} | {c['actual']:.2f} | "
                    f"{c['diff']:+.4f} | {mark} |")
    price_bad = any(x["level"] == "blocker" for x in report["price"]["detail"])
    rejects = report["orders"].get("rejects", 0)
    lines = [
        f"## 账本比对 {report['day']} {report['symbol']}",
        f"拟下单 {n['signals']}（拒单 {rejects}）/ 订单 {n['orders']} / 成交 {n['fills']}"
        f" ⇒ **{'一致' if report['verdict'] == 'ok' else '不一致'}**",
        "",
        "| 校验 | 重算 | 账面 | 差 | 结论 |",
        "|---|---|---|---|---|",
        cash_row,
        f"| 持仓 | {p['from_fills']} 股 | {p['in_state']} 股 | {p['diff']:+d} | "
        f"{'✅' if p['ok'] else '❌'} |",
        f"| 成交价在 bar 内 | {report['price']['checked']} 笔已查 | — | — | "
        f"{'❌' if price_bad else '✅'} |",
    ]
    if report["price"]["detail"]:
        lines += ["", "| 成交时刻 | 价格 | 区间内 | 余量(低/高) | 备注 |", "|---|---|---|---|---|"]
        for x in report["price"]["detail"]:
            ml = "—" if x["margin_lo_pct"] is None else f"{x['margin_lo_pct']:+.3f}%"
            mh = "—" if x["margin_hi_pct"] is None else f"{x['margin_hi_pct']:+.3f}%"
            lines.append(f"| {x['filled_at'][:16]} | {x['price']} | "
                         f"{'是' if x['inside'] else ('否' if x['inside'] is False else '未知')} | "
                         f"{ml} / {mh} | {x['note'] or '—'} |")
    if report["blockers"]:
        lines += ["", "**阻断项**"] + [f"- {b}" for b in report["blockers"]]
    if report["warnings"]:
        lines += ["", "**待确认**"] + [f"- {w}" for w in report["warnings"]]
    return "\n".join(lines)


# ── 读盘胶水（只读） ───────────────────────────────────────────────────
def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    p = Path(path)
    if not p.exists():
        return out
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def load_json(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def audit_unit(unit_dir: str | Path, csv_path: str | Path, day: str,
               symbol: str) -> dict[str, Any]:
    """对一个 live_outputs/<标的>/<策略>/ 目录做当日比对。"""
    unit = Path(unit_dir)
    state = load_json(unit / "paper_state.json")
    orders = state.get("orders") or []
    fills = state.get("fills") or []
    signals = load_jsonl(unit / "signals.jsonl")
    bars = load_bars(csv_path) if Path(csv_path).exists() else {}
    return audit_day(day=day, symbol=symbol, signals=signals, orders=orders,
                     fills=fills, state=state, bars=bars)
