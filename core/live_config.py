# -*- coding: utf-8 -*-
"""live 配置层：把「标的 / 策略 / 参数 / 启停」从源码里搬出来。

## 为什么需要它

当前 live 的组合是**硬编码**的：

    # live_match_loop.py:45
    COMBOS = [("300308", "grid_trade"), ("300308", "ma_swing")]

而回测侧（`app.py`）早已支持「全市场搜标的 + 策略注册表动态读取 + ParamMeta 自动生成参数控件」。
两侧能力不对等 ⇒ 用户想"随时挑标的、换策略、改参数"必须改源码重启。

本模块只做**一件事**：定义一份可读可校验的配置文件 `data/live_config.json`，
作为 live 侧的唯一真相源。**不接线任何执行逻辑**（接线由 live_match_loop 另行决定），
因此本模块上线**不改变任何现有行为**。

## 设计约束

1. **字段白名单**：参数覆盖的 key 必须存在于该策略的 `default_params()`，
   否则静默失效（这正是 §6.9「机制存在 ≠ 被调用」的高发区）。
2. **值域校验**：若 `ParamMeta` 给了 `min`/`max`，越界即报错而不是截断。
3. **唯一性**：同一 `(code, strategy_id)` 不允许重复，避免两个观察器抢同一个状态目录。
4. **向后兼容**：配置文件不存在时 `load()` 返回 `None`，调用方可回退到原 COMBOS。
5. **写入原子性**：先写临时文件再 `replace`（H48/H50 幂等教训）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

APP = Path(__file__).resolve().parents[1]
if str(APP.parent) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(APP.parent))

CONFIG_PATH = APP / "data" / "live_config.json"
SCHEMA_VERSION = 1

CODE_RE = re.compile(r"^\d{6}$")


@dataclass
class LivePosition:
    """一个「标的 × 策略」观察单元。"""
    code: str
    strategy_id: str
    enabled: bool = True
    param_overrides: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        return {"code": self.code, "strategy_id": self.strategy_id,
                "enabled": bool(self.enabled),
                "param_overrides": dict(self.param_overrides),
                "note": self.note}


@dataclass
class LiveConfig:
    version: int = SCHEMA_VERSION
    updated_at: str = ""
    initial_cash: Optional[float] = None      # None ⇒ 用策略默认（纸面 100 万）
    risk_pct: float = 0.10
    positions: list = field(default_factory=list)
    risk: dict = field(default_factory=dict)  # 覆盖 RiskConfig 的字段
    # 纸面交易起点（YYYY-MM-DD）。None ⇒ 回落到模块常量 TRADE_START_DATE。
    # 2026-10-28：原起点硬编码 2026-09-07，导致纸面永远比实盘早跑 50 天
    # （底仓成本 303.16 vs 现价 278，账面 -3% 属历史包袱，不是真实成绩）。
    paper_start_date: Optional[str] = None
    # H72：live 采集主源。ad = 星耀（默认历史口径）/ qmt = 迅投本地客户端。
    # QMT 失败会自动回退 AD；AD 的"单点会话"约束（H34/H37/H44）在 QMT 上不存在。
    kline_source: str = "qmt"
    # H73：**跨策略**单标总持仓上限（占总资金比例）。
    # None ⇒ 不启用（历史行为：每个「标的×策略」各有一份 initial_cash，
    # 四策略跑同一只票相当于 4 倍资金 —— 纸面能跑，实盘同一账户会叠加，接实盘前必须开）。
    # 启用后：同一标的在**所有策略合计**的持仓市值不得超过 cross_symbol_cap × initial_cash。
    cross_symbol_cap: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "updated_at": self.updated_at or datetime.now().isoformat(timespec="seconds"),
            "initial_cash": self.initial_cash,
            "risk_pct": self.risk_pct,
            "positions": [p.to_dict() for p in self.positions],
            "risk": dict(self.risk),
            "paper_start_date": self.paper_start_date,
            "kline_source": self.kline_source,
            "cross_symbol_cap": self.cross_symbol_cap,
        }


# ── 校验 ─────────────────────────────────────────────────────────────────────

def _strategy_registry() -> dict:
    """延迟导入，避免本模块被 import 时拖入策略依赖。"""
    from strategies import STRATEGIES
    return STRATEGIES


def validate(cfg: LiveConfig) -> list[str]:
    """返回错误列表（空 = 通过）。**不抛异常**，便于 UI 直接展示。"""
    from strategies import STRATEGIES

    errs: list[str] = []

    if not (0 < float(cfg.risk_pct) <= 1):
        errs.append(f"risk_pct 必须在 (0, 1] 内，当前 {cfg.risk_pct}")
    if cfg.initial_cash is not None and float(cfg.initial_cash) <= 0:
        errs.append(f"initial_cash 必须 > 0，当前 {cfg.initial_cash}")

    if str(cfg.kline_source).strip().lower() not in ("ad", "qmt", "stockdb"):
        errs.append(f"kline_source 只能是 ad / qmt / stockdb，当前 {cfg.kline_source!r}")

    # H73：跨策略上限。None = 关闭；开了必须在 (0, 1] 且 initial_cash 必须显式给出
    # （没有资金基准就没法算"占比"，静默按默认 100 万算等于埋雷）。
    if cfg.cross_symbol_cap is not None:
        try:
            cap = float(cfg.cross_symbol_cap)
        except (TypeError, ValueError):
            errs.append(f"cross_symbol_cap 必须是数字，当前 {cfg.cross_symbol_cap!r}")
        else:
            if not (0 < cap <= 1):
                errs.append(f"cross_symbol_cap 必须在 (0, 1] 内，当前 {cap}")
        if cfg.initial_cash is None:
            errs.append("启用 cross_symbol_cap 必须同时给出 initial_cash（否则占比无从计算）")

    # 纸面起点：格式必须是 YYYY-MM-DD，且不能在未来（未来日没有可回放的行情）
    if cfg.paper_start_date:
        try:
            _start = datetime.strptime(str(cfg.paper_start_date), "%Y-%m-%d").date()
        except ValueError:
            errs.append(f"paper_start_date 必须是 YYYY-MM-DD，当前 {cfg.paper_start_date!r}")
        else:
            if _start > datetime.now().date():
                errs.append(f"paper_start_date={_start} 在未来，没有行情可回放")

    seen: set = set()
    for i, p in enumerate(cfg.positions):
        tag = f"positions[{i}]({p.code}/{p.strategy_id})"
        if not CODE_RE.match(str(p.code)):
            errs.append(f"{tag}: 代码必须是 6 位数字")
            continue
        if p.strategy_id not in STRATEGIES:
            errs.append(f"{tag}: 策略未注册，可选 {list(STRATEGIES)}")
            continue

        key = (p.code, p.strategy_id)
        if key in seen:
            errs.append(f"{tag}: 同一标的+策略重复配置（会争抢同一状态目录）")
        seen.add(key)

        # 参数覆盖：key 必须在白名单内，值必须在 ParamMeta 值域内
        try:
            strat = STRATEGIES[p.strategy_id]()
            allowed = set(strat.default_params())
            # ⚠️ `param_meta` 是**类属性 list**，不是方法（strategies/base.py:59）
            metas = {m.key: m for m in (getattr(strat, "param_meta", None) or [])}
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{tag}: 无法实例化策略（{type(exc).__name__}: {exc}）")
            continue

        for k, v in (p.param_overrides or {}).items():
            if k not in allowed:
                errs.append(f"{tag}: 未知参数 '{k}'（该策略合法参数：{sorted(allowed)}）")
                continue
            m = metas.get(k)
            if m is not None and isinstance(v, (int, float)):
                if m.min is not None and v < m.min:
                    errs.append(f"{tag}: 参数 {k}={v} 低于下限 {m.min}")
                if m.max is not None and v > m.max:
                    errs.append(f"{tag}: 参数 {k}={v} 高于上限 {m.max}")
    return errs


# ── 读写 ─────────────────────────────────────────────────────────────────────

def from_dict(d: dict) -> LiveConfig:
    pos = [LivePosition(code=str(p.get("code", "")),
                        strategy_id=str(p.get("strategy_id", "")),
                        enabled=bool(p.get("enabled", True)),
                        param_overrides=dict(p.get("param_overrides") or {}),
                        note=str(p.get("note", "")))
           for p in (d.get("positions") or [])]
    return LiveConfig(version=int(d.get("version", SCHEMA_VERSION)),
                      updated_at=str(d.get("updated_at", "")),
                      initial_cash=d.get("initial_cash"),
                      risk_pct=float(d.get("risk_pct", 0.10)),
                      positions=pos,
                      risk=dict(d.get("risk") or {}),
                      paper_start_date=(str(d["paper_start_date"]).strip()
                                        if d.get("paper_start_date") else None),
                      kline_source=str(d.get("kline_source") or "qmt").strip().lower(),
                      cross_symbol_cap=(float(d["cross_symbol_cap"])
                                        if d.get("cross_symbol_cap") is not None else None))


def load(path: Path | None = None) -> LiveConfig | None:
    """读配置。文件不存在 ⇒ None（调用方回退到原 COMBOS）。损坏 ⇒ 抛，不静默吞。"""
    p = Path(path or CONFIG_PATH)
    if not p.exists():
        return None
    raw = json.loads(p.read_text(encoding="utf-8"))
    return from_dict(raw)


def save(cfg: LiveConfig, path: Path | None = None) -> Path:
    """原子写：先写 .tmp 再 replace。校验不通过时**拒绝写入**。"""
    errs = validate(cfg)
    if errs:
        raise ValueError("配置校验未通过：\n" + "\n".join(f"  - {e}" for e in errs))
    p = Path(path or CONFIG_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    cfg.updated_at = datetime.now().isoformat(timespec="seconds")
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


# ── 供执行侧消费的视图 ───────────────────────────────────────────────────────

def to_combos(cfg: LiveConfig) -> list[tuple[str, str]]:
    """等价于原来的 COMBOS，但只含 enabled=True 的项。"""
    return [(p.code, p.strategy_id) for p in cfg.positions if p.enabled]


def overrides_for(cfg: LiveConfig, code: str, strategy_id: str) -> dict:
    """取某单元的参数字典覆盖（未配置 ⇒ 空 dict）。"""
    for p in cfg.positions:
        if p.code == code and p.strategy_id == strategy_id:
            return dict(p.param_overrides or {})
    return {}


def effective_params(cfg: LiveConfig, code: str, strategy_id: str) -> dict:
    """默认参数 + 覆盖 ⇒ 最终参数（供预览/回测对比用）。"""
    from strategies import get_strategy
    base = get_strategy(strategy_id).default_params()
    base.update(overrides_for(cfg, code, strategy_id))
    return base
