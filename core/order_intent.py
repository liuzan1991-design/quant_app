"""委托归属：本地意图表 + 柜台 sysid 固化。

为什么不能用 order_id
    2026-10-28 真实账户只读快照（7 委托 / 11 成交，`probe_20261028_085035.json`）：
    `query_stock_orders` 返回的 `order_id` 与 `m_nOrderID` **7/7 全为 0**，
    而 `order_stock()` 返回的是一个本地会话号，没有任何字段能与查询侧对上。
    若归属按 order_id 往返，重启后必然把"自己刚下的单"判成历史单 ——
    后果是重复下单。

可用的键
    `m_strOrderSysID`（交易所委托编号）下单前不存在，一旦产生就稳定；
    实测能把 1 笔委托 ↔ N 笔成交正确串起来（11 笔成交按 sysid 汇总，
    7/7 与委托量相等，如 700 = 600+100）。

归属三分法（失败关闭）
    owned     本地意图 ↔ 柜台委托 一对一，绑定后按 sysid 固化，重启不再重算
    ambiguous 候选 >1，或两个意图属性完全相同无法区分 ⇒ **必须停手**，不得猜
    orphan    柜台有单但无对应意图 ⇒ 非本策略（手工/其他终端/前次会话遗留）

remark 只作线索，不作证据
    手工单回读为空串（实测 7/7），所以「空 remark ⇒ 非本策略」可用；
    但「程序化下单的 remark 一定能回读」**未验**（本机尚无 xttrader 下单记录，
    miniQMT 处于停用状态）。因此 remark 命中可直接判 owned，
    remark 未命中仍须走属性校验 —— 不能反推"没 remark 就不是自己的单"。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

BEIJING = timezone(timedelta(hours=8))
REMARK_PREFIX = "QA"     # 纯 ASCII：沙箱口径要求零中文
REMARK_MAX_LEN = 12      # 保守：柜台备注长度上限未实测，宁短勿长

SIDE_BUY, SIDE_SELL = "BUY", "SELL"
# 迅投 ORDER.m_nOffsetFlag：48=买入 49=卖出。**不用 m_nDirection**（股票常为固定值）
OFFSET_FLAG = {48: SIDE_BUY, 49: SIDE_SELL}

DEFAULT_PRICE_TOL_PCT = 0.02     # 限价单价格比对容差（%），标定见下
DEFAULT_TIME_WINDOW_SEC = 30     # 意图时刻与委托时刻的允许偏差（秒）

# 标定依据（2026-10-28 真实账户，同标的同方向 4 笔委托）：
#   相邻价格差 0.101% / 0.231% / 0.851%，相邻时间差 41s / 59s / 278s。
#   ⇒ 价格容差取**最小价差的 1/5**（0.02%），时间窗取**小于最小时差的 30s**，
#     真实数据上 4 笔互不混淆。
# ⚠ 初版取 0.2% / 180s，在真实数据上把 1557 与 1569（差 0.13%、隔 100s）判成
#   同一候选 ⇒ 归属失败关闭、策略停手。容差过松不是"更安全"，是把正常单判死。


class IntentError(ValueError):
    """意图/委托入参不合法（不是业务歧义）。"""


@dataclass(frozen=True)
class Intent:
    intent_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    strategy: str
    created_at: str          # 北京时间 ISO，秒精度
    remark: str

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


def _norm_symbol(value: Any) -> str:
    s = str(value or "").strip().upper()
    if not s:
        raise IntentError("标的代码为空")
    if "." not in s and len(s) == 6 and s.isdigit():
        s += ".SZ" if s[0] in "03" else ".SH"
    return s


def _norm_side(value: Any) -> str:
    s = str(value or "").strip().upper()
    if s not in (SIDE_BUY, SIDE_SELL):
        raise IntentError(f"方向必须为 BUY/SELL：{value!r}")
    return s


def new_intent(*, symbol: str, side: str, quantity: int, price: float,
               strategy: str, created_at: str | None = None,
               intent_id: str | None = None) -> Intent:
    """建一条下单意图。remark = 'QA' + 8 位十六进制（共 10 字符）。"""
    qty = int(quantity)
    if qty <= 0:
        raise IntentError(f"数量必须为正：{quantity!r}")
    px = float(price)
    if px <= 0:
        raise IntentError(f"价格必须为正：{price!r}")
    iid = (intent_id or uuid.uuid4().hex[:8]).lower()
    if len(iid) < 4:
        raise IntentError(f"intent_id 过短：{iid!r}")
    stamp = created_at or datetime.now(BEIJING).strftime("%Y-%m-%dT%H:%M:%S")
    return Intent(intent_id=iid, symbol=_norm_symbol(symbol), side=_norm_side(side),
                  quantity=qty, price=px, strategy=str(strategy or ""),
                  created_at=str(stamp)[:19],
                  remark=f"{REMARK_PREFIX}{iid}"[:REMARK_MAX_LEN])


def _parse_time(value: Any) -> datetime | None:
    """接受 ISO 字符串或 epoch 秒；统一返回北京时间。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), BEIJING)
    s = str(value).strip()
    try:
        dt = datetime.fromisoformat(s.replace("/", "-"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=BEIJING)


def normalize_qmt_order(raw: Mapping[str, Any]) -> dict[str, Any]:
    """把迅投委托对象/字典规整成归属匹配用的最小结构。

    字段优先级：带 `m_` 前缀的原生字段 → 无前缀的 xtquant 解析字段。
    方向只认 `m_nOffsetFlag`；缺失即拒（不猜）。
    """
    flag = raw.get("m_nOffsetFlag", raw.get("offset_flag"))
    try:
        side = OFFSET_FLAG.get(int(flag))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        side = None
    if side is None:
        raise IntentError(f"无法从 m_nOffsetFlag 判定方向：{flag!r}")

    qty = raw.get("m_nOrderVolume", raw.get("order_volume"))
    price = raw.get("m_dPrice", raw.get("price"))
    ts = raw.get("m_nOrderTime", raw.get("order_time"))
    if isinstance(ts, str) and ts.isdigit():
        ts = int(ts)
    return {
        "sysid": str(raw.get("m_strOrderSysID", raw.get("order_sysid")) or "").strip(),
        "symbol": _norm_symbol(raw.get("m_strStockCode", raw.get("stock_code"))),
        "side": side,
        "quantity": int(qty or 0),
        "price": float(price or 0.0),
        "time": _parse_time(ts),
        "time_raw": ts,
        "remark": str(raw.get("m_strOrderRemark", raw.get("order_remark")) or ""),
        "strategy": str(raw.get("m_strStrategyName", raw.get("strategy_name")) or ""),
        # order_id 保留仅为观测：柜台实测恒为 0，不参与匹配
        "order_id": raw.get("order_id", raw.get("m_nOrderID")),
    }


@dataclass
class BindResult:
    matched: dict[str, dict[str, Any]] = field(default_factory=dict)   # intent_id -> {sysid, by}
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    orphan_orders: list[dict[str, Any]] = field(default_factory=list)
    unbound: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """有歧义即不可放行（失败关闭）。"""
        return not self.ambiguous

    def sysid_of(self, intent_id: str) -> str | None:
        hit = self.matched.get(intent_id)
        return hit.get("sysid") if hit else None

    def summary(self) -> str:
        return (f"owned={len(self.matched)} ambiguous={len(self.ambiguous)} "
                f"orphan={len(self.orphan_orders)} unbound={len(self.unbound)}")


def _price_close(a: float, b: float, tol_pct: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / b * 100.0 <= tol_pct


def _time_close(a: datetime | None, b: datetime | None, window_sec: int) -> bool:
    if a is None or b is None:
        return False
    return abs((a - b).total_seconds()) <= window_sec


def bind(intents: Sequence[Intent], orders: Sequence[Mapping[str, Any]], *,
         price_tol_pct: float = DEFAULT_PRICE_TOL_PCT,
         time_window_sec: int = DEFAULT_TIME_WINDOW_SEC,
         frozen: Mapping[str, str] | None = None) -> BindResult:
    """把意图绑到柜台委托上。frozen = {intent_id: sysid} 为已固化绑定，优先复用。

    不修改入参；返回新结构。任何不确定都归 ambiguous，不做启发式抢占。
    """
    result = BindResult()
    by_sysid = {str(o["sysid"]): o for o in orders if o.get("sysid")}
    used: set[str] = set()

    # 0) 已固化绑定：sysid 仍在且属性不冲突才认，否则重新匹配（不沿用脏绑定）
    if frozen:
        for intent in intents:
            sid = frozen.get(intent.intent_id)
            if not sid or sid not in by_sysid or sid in used:
                continue
            order = by_sysid[sid]
            if order["symbol"] != intent.symbol or order["side"] != intent.side:
                continue
            result.matched[intent.intent_id] = {"sysid": sid, "by": "frozen"}
            used.add(sid)

    pending = [i for i in intents if i.intent_id not in result.matched]

    # 1) remark 精确命中（高置信，但仍要求品种/方向一致）
    still: list[Intent] = []
    for intent in pending:
        cands = [o for o in orders
                 if o.get("sysid") and o["sysid"] not in used
                 and o.get("remark") == intent.remark and intent.remark
                 and o["symbol"] == intent.symbol and o["side"] == intent.side]
        if len(cands) == 1:
            sid = cands[0]["sysid"]
            result.matched[intent.intent_id] = {"sysid": sid, "by": "remark"}
            used.add(sid)
        elif len(cands) > 1:
            result.ambiguous.append({"intent_id": intent.intent_id, "by": "remark",
                                     "candidates": [c["sysid"] for c in cands]})
        else:
            still.append(intent)

    # 2) 属性匹配：代码 + 方向 + 数量 + 价格容差 + 时间窗
    cand_map: dict[str, list[str]] = {}
    for intent in still:
        t_intent = _parse_time(intent.created_at)
        cands = [o for o in orders
                 if o.get("sysid") and o["sysid"] not in used
                 and o["symbol"] == intent.symbol and o["side"] == intent.side
                 and int(o["quantity"]) == intent.quantity
                 and _price_close(float(o["price"]), intent.price, price_tol_pct)
                 and _time_close(o.get("time"), t_intent, time_window_sec)]
        cand_map[intent.intent_id] = [c["sysid"] for c in cands]

    claims: dict[str, list[str]] = {}
    for iid, sids in cand_map.items():
        for sid in sids:
            claims.setdefault(sid, []).append(iid)

    for intent in still:
        sids = cand_map[intent.intent_id]
        if not sids:
            result.unbound.append(intent.intent_id)
            continue
        if len(sids) > 1:
            result.ambiguous.append({"intent_id": intent.intent_id, "by": "attrs",
                                     "candidates": sorted(sids)})
            continue
        sid = sids[0]
        if len(claims.get(sid, [])) > 1:
            # 一单多意图：无法区分谁下的，双方都停手
            result.ambiguous.append({"intent_id": intent.intent_id, "by": "shared_order",
                                     "candidates": [sid]})
            continue
        result.matched[intent.intent_id] = {"sysid": sid, "by": "attrs"}
        used.add(sid)

    result.orphan_orders = [o for o in orders if o.get("sysid") and o["sysid"] not in used]
    return result


def to_frozen_records(result: BindResult, *, stamp: str | None = None) -> list[dict[str, Any]]:
    """把本次 owned 绑定固化成可落盘的记录（只含稳定键，不含行情/价格猜测）。"""
    at = stamp or datetime.now(BEIJING).strftime("%Y-%m-%dT%H:%M:%S")
    return [{"intent_id": iid, "sysid": hit["sysid"], "by": hit["by"], "bound_at": at}
            for iid, hit in sorted(result.matched.items())]


def load_frozen(path: str | Path) -> dict[str, str]:
    """读固化表，返回 {intent_id: 最新 sysid}；坏行跳过，不中断。"""
    p = Path(path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            iid, sid = rec.get("intent_id"), rec.get("sysid")
            if iid and sid:
                out[str(iid)] = str(sid)
    return out


def append_frozen(path: str | Path, records: Iterable[Mapping[str, Any]]) -> int:
    """追加固化记录（只追加，不改写已有行）。返回写入条数。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(p, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(dict(rec), ensure_ascii=False) + "\n")
            n += 1
    return n
