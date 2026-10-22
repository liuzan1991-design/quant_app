# -*- coding: utf-8 -*-
"""公司行为（除权除息）事件数据层 —— H40（2026-10-21 拍板：税前入账留痕、成本冲减、历史缺口不回填）。

数据源：东财 datacenter ``RPT_SHAREBONUS_DET``（主源，提供送转/派息的权威分解；
字段实测：EX_DIVIDEND_DATE / PRETAX_BONUS_RMB(每10股税前) / BONUS_IT_RATIO(每10股送转合计,纯分红为 null)）。
交叉校验：AD 后复权因子跳变日（core/adjustment.py 现成检测）——单因子是派息/送转的复合值，
无法区分形态（欠定），故仅作弱校验，不一致告警不阻断。
缓存：data/corporate_actions_<code>.json；换日应用只读缓存（盘中零网络 IO），
刷新由 live_run 启动段触发（失败降级旧缓存）。
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.adjustment import load_backward_factor  # 无环：adjustment 不依赖本模块

_LOGGER = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CACHE_TTL_DAYS = 7  # 除权实施公告通常早于除权日 1~2 周；7 天足够新，又不必每天拉
_EASTMONEY_BASE = (
    "https://datacenter-web.eastmoney.com/api/data/v1/get"
    "?reportName=RPT_SHAREBONUS_DET&columns=ALL"
    "&filter={flt}"
    "&sortColumns=EX_DIVIDEND_DATE&sortTypes=-1&pageSize=100&pageNumber=1"
)


def build_url(code: str) -> str:
    """构造东财请求 URL。

    🚨 H48：filter 里含中文「实施分配」，而 ``http.client`` 的请求行**只能**
    ``encode('ascii')`` ⇒ 裸中文必抛 ``UnicodeEncodeError``（生产首日 2026-10-22
    04:32:08 实测，position 112-115 正好是这 4 个汉字）。
    ⇒ 必须整体百分号编码（``safe=""`` 连 ``=`` ``"`` 一起编码，服务端实测可解析）。
    """
    flt = '(SECURITY_CODE="%s")(ASSIGN_PROGRESS="实施分配")' % code
    return _EASTMONEY_BASE.format(flt=urllib.parse.quote(flt, safe=""))


@dataclass(frozen=True)
class CorporateAction:
    """一次除权除息事件。分红/送转基数均为登记日（送转前）持股。"""

    ex_date: str  # YYYY-MM-DD（ISO，与 broker trading_day 同格式，可直接字符串比较）
    dividend_per_share: float  # 每股税前派息（元）；纯送转为 0
    bonus_ratio: float  # 每股送转比例（10转4 → 0.4）；纯分红为 0
    plan_desc: str = ""  # 原始方案描述，留痕用


def cache_path(code: str) -> Path:
    return DATA_DIR / f"corporate_actions_{code}.json"


def symbol_to_code(symbol: str) -> str:
    """"300308.SZ" -> "300308"；裸 code 原样返回。"""
    return symbol.split(".")[0]


def _parse_payload(payload: dict) -> List[CorporateAction]:
    """解析东财返回 JSON（纯函数，便于离线测试）。"""
    rows = ((payload or {}).get("result") or {}).get("data") or []
    actions: List[CorporateAction] = []
    for row in rows:
        ex_date = str(row.get("EX_DIVIDEND_DATE") or "")[:10]
        if not ex_date:
            continue
        dividend = float(row.get("PRETAX_BONUS_RMB") or 0.0) / 10.0  # 每10股 → 每股
        bonus = float(row.get("BONUS_IT_RATIO") or 0.0) / 10.0
        actions.append(CorporateAction(
            ex_date=ex_date,
            dividend_per_share=dividend,
            bonus_ratio=bonus,
            plan_desc=str(row.get("IMPL_PLAN_PROFILE") or ""),
        ))
    # 同日可能多条（历史数据分页），按 ex_date 去重保留最后一条（排序 -1 ⇒ 最新）
    dedup: Dict[str, CorporateAction] = {}
    for action in actions:
        dedup[action.ex_date] = action
    return [dedup[k] for k in sorted(dedup)]


def fetch_actions(code: str, timeout: int = 15) -> List[CorporateAction]:
    """拉取东财全量实施分配记录（网络）。"""
    url = build_url(code)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return _parse_payload(payload)


def load_cached(code: str) -> Optional[List[CorporateAction]]:
    """读本地缓存；不存在/损坏返回 None。"""
    path = cache_path(code)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [CorporateAction(**item) for item in payload.get("actions", [])]
    except (json.JSONDecodeError, TypeError, KeyError):
        _LOGGER.warning("公司行为缓存损坏，视为缺失 path=%s", path)
        return None


def refresh_cache(code: str, force: bool = False,
                  ttl_days: int = _CACHE_TTL_DAYS) -> Tuple[int, bool]:
    """刷新缓存。返回 (事件数, 是否本次新拉取)。拉取失败抛异常（调用方决定降级策略）。"""
    import time as _time

    path = cache_path(code)
    if not force and path.exists():
        age_days = (_time.time() - path.stat().st_mtime) / 86400.0
        if age_days < ttl_days:
            cached = load_cached(code)
            return (len(cached) if cached else 0), False
    actions = fetch_actions(code)
    payload = {
        "code": code,
        "refreshed_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "eastmoney RPT_SHAREBONUS_DET",
        "actions": [action.__dict__ for action in actions],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    verify_against_factor(code, actions)  # 弱校验：只告警
    return len(actions), True


def pending_from_cache(code: str, after_date: str,
                       upto_date: str) -> List[CorporateAction]:
    """取 (after_date, upto_date] 窗口内、缓存中已知的除权事件（只读缓存，零网络）。"""
    actions = load_cached(code)
    if not actions:
        return []
    return [a for a in actions if after_date < a.ex_date <= upto_date]


def verify_against_factor(code: str, actions: List[CorporateAction],
                          tolerance: float = 1e-8) -> None:
    """弱校验：东财除权日集合 vs AD 后复权因子跳变日集合，差异仅告警不阻断。"""
    try:
        factor = load_backward_factor(code)
        if factor is None or len(factor) < 2:
            _LOGGER.info("因子数据不足，跳过公司行为交叉校验 code=%s", code)
            return
        day_factor = factor.groupby(factor.index).last()
        changed = {ts.date().isoformat() for ts, delta in
                   day_factor.pct_change().abs().items() if delta > tolerance}
    except Exception:  # noqa: BLE001 —— 校验失败不影响主流程
        _LOGGER.warning("公司行为交叉校验异常（忽略） code=%s", code)
        return
    east_dates = {a.ex_date for a in actions}
    # 因子覆盖期有限（缓存起点之后），只对双方都有信息的窗口做双向差集
    only_east = {d for d in east_dates - changed
                 if d >= min(changed, default="9999-12-31")}
    only_factor = {d for d in changed - east_dates
                   if d >= min(east_dates, default="9999-12-31")}
    if only_east:
        _LOGGER.warning("东财有除权但因子未跳变（数据源差异，请人工核对） code=%s dates=%s",
                        code, sorted(only_east))
    if only_factor:
        _LOGGER.warning("因子跳变但东财无除权记录（数据源差异，请人工核对） code=%s dates=%s",
                        code, sorted(only_factor))
