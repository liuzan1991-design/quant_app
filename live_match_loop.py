# -*- coding: utf-8 -*-
"""实时增量撮合循环（观察期主程序）。

只读 `data/live/` 下采集进程落盘的最新 1 分钟行情，不碰 AmazingData SDK。
观察两个真增量组合：中际旭创×网格、中际旭创×均线波段。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_LOGGER = logging.getLogger(__name__)

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.live_params import freeze_params, INIT_CASH  # noqa: E402
from core.adjustment import prepare_signal_prices  # noqa: E402
from core.grid_shared_live import GridSharedLiveEngine  # noqa: E402
from core.ma_swing_live import MaSwingLiveEngine  # noqa: E402
from core.intraday_t_shared_live import IntradayTSharedLiveEngine  # noqa: E402  （H65）
from core.sentiment_t_shared_live import SentimentTSharedLiveEngine  # noqa: E402  （H71）
from core.paper_broker import PaperBroker, PaperBrokerConfig  # noqa: E402
from core.paper_replay import OfflineReplayEngine, StrategySignal  # noqa: E402
from core.data_quality import day_bar_stats  # noqa: E402  （H35 缺口标记）
from core import live_config  # noqa: E402  （H57 配置驱动）
from core import cross_risk  # noqa: E402  （H73 跨策略闸门）
from core.position_cap import evaluate as cap_evaluate, grade as cap_grade, CAP_KIND  # noqa: E402  （H46-③）
from core.alerts import clear_alert, raise_alert  # noqa: E402  （H46-③ 送达）
from core.paper_store import PaperStateStore  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager  # noqa: E402
from core.price_limit import add_limit_columns  # noqa: E402  （H61 涨跌停）
from core.data import stock_name  # noqa: E402  （H61 ST 判定）
from strategies.grid_trade import GridTradeStrategy  # noqa: E402
from strategies.ma_swing import MaSwingStrategy  # noqa: E402
from strategies.intraday_t import IntradayTStrategy  # noqa: E402  （H65）
from strategies.sentiment_t import SentimentTStrategy  # noqa: E402  （H71）

LIVE_DIR = APP_DIR / "data" / "live"
OUTPUT_DIR = APP_DIR / "live_outputs"

# 观察期统一交易起点：底仓建仓价、信号、成交都从这天起；之前是预热段只喂指标。
# ⚠️ 这只是**兜底常量**：实际起点由 `_trade_start_date(cfg)` 决定（H67）。
TRADE_START_DATE = pd.Timestamp("2026-09-07")


def _trade_start_date(cfg=None) -> pd.Timestamp:
    """纸面交易起点：`live_config.paper_start_date` 优先，其次模块兜底常量。

    2026-10-28（用户拍板 A）：原起点硬编码 09-07，纸面永远比实盘早跑 50 天
    —— 底仓成本 303.16 vs 现价 278，账面 -3% 是历史包袱、不是真实成绩。
    改为「首次生效日」后才反映真实跟踪；无配置/字段为空 ⇒ 行为逐字节不变
    （回测与既有用例不受影响）。
    """
    raw = getattr(cfg, "paper_start_date", None) if cfg is not None else None
    if raw:
        try:
            return pd.Timestamp(str(raw))
        except (ValueError, TypeError):   # 脏数据不阻断启动：回落兜底并留痕
            _LOGGER.warning("[H67] paper_start_date=%r 无法解析，回落 %s",
                            raw, TRADE_START_DATE.date())
    return TRADE_START_DATE

COMBOS = [
    ("300308", "grid_trade"),
    ("300308", "ma_swing"),
]

# H57-S3：live 目前支持的策略（`_engine_for` 有对应分支）。
# 配置里出现其它策略必须**明确报错**，不能静默跳过 —— 否则用户以为换上了，实际没跑。
# H65/H71（2026-10-28）：面板可选的 live 策略。
# H71 更正 H65：`sentiment_data._fetch_and_cache` 是**缓存优先** —— 缓存命中直接返回、
# 不碰 AD；只有缺口才调 fetch_kline，且复用 live 进程内同一会话（不是"额外登录"）。
# 真实代价：启动时补指数缺口（缺几天补几天），之后纯内存查表。
# 降级风险：指数加载失败时 status_map 为空 ⇒ 退化为普通做T，**必须留痕不能静默**。
_SUPPORTED_LIVE_STRATEGIES = {"grid_trade", "ma_swing", "intraday_t", "sentiment_t"}


def _resolve_combos(cfg=None) -> list[tuple[str, str]]:
    """组合来源。**无配置文件 ⇒ 原 COMBOS（行为逐字节不变）**。"""
    cfg = cfg if cfg is not None else live_config.load()
    if cfg is None:
        return list(COMBOS)
    combos = live_config.to_combos(cfg)
    if not combos:
        raise RuntimeError(
            "live_config.json 中没有任何 enabled 的组合 —— 拒绝空跑。"
            "若想停掉实盘，请直接停进程/禁任务，而不是把配置清空。")
    unsupported = sorted({sid for _, sid in combos
                          if sid not in _SUPPORTED_LIVE_STRATEGIES})
    if unsupported:
        raise RuntimeError(
            f"配置含 live 尚未支持的策略 {unsupported}；"
            f"当前仅支持 {sorted(_SUPPORTED_LIVE_STRATEGIES)}（补齐见 H57-S4）。"
            f"拒绝静默跳过，否则会误以为已在运行。")
    return combos


def _record_applied_config(cfg, combos) -> None:
    """H57-S3 最小护栏：每次 live 启动把**实际生效**的组合与参数覆盖落盘。

    为什么必须有：配置一旦能在界面里改，"今天到底按什么参数跑的"就变成
    必须可追溯的事实（否则事后无法解释差异）。这是 S5 完整审计的雏形。
    """
    try:
        payload = {
            "applied_at": datetime.now().isoformat(timespec="seconds"),
            "source": "live_config.json" if cfg is not None else "COMBOS(源码默认)",
            "combos": [list(c) for c in combos],
            "overrides": ({f"{c}×{s}": live_config.overrides_for(cfg, c, s)
                           for c, s in combos} if cfg is not None else {}),
            # H64：实盘口径也留痕 —— 资金基准与风控上限改动会直接改变下单规模，
            # 事后必须能从这一行解释"今天为什么下了这个量"。
            "initial_cash": (cfg.initial_cash if cfg is not None else None),
            "risk_pct": (cfg.risk_pct if cfg is not None else None),
            "risk": (dict(cfg.risk) if cfg is not None else {}),
        }
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        (LIVE_DIR / "config_applied.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 —— 留痕失败不得影响启动
        _LOGGER.warning("H57: 写入 config_applied.json 失败（忽略）", exc_info=True)


# H65：策略类注册表。**所有需要 default_params 的地方都从这里取**，
# 否则新增策略时 else 分支会把 ma_swing 的默认参数错给别的策略。
_STRATEGY_CLASSES = {
    "grid_trade": GridTradeStrategy,
    "ma_swing": MaSwingStrategy,
    "intraday_t": IntradayTStrategy,
    "sentiment_t": SentimentTStrategy,
}


def _params_for(cfg, code: str, sid: str, first_price: float) -> dict:
    """默认参数 → freeze_params → 叠加配置覆盖（覆盖优先）。

    H64：`init_cash` / `risk_pct` 来自 live_config（无配置 ⇒ 用模块常量，行为不变）。
    """
    cls = _STRATEGY_CLASSES.get(sid)
    if cls is None:
        raise RuntimeError(f"未注册的 live 策略 {sid}（见 _STRATEGY_CLASSES）")
    base_params = cls.default_params()
    params = freeze_params(
        sid, first_price, base_params,
        init_cash=(cfg.initial_cash if cfg is not None else None),
        risk_pct=(cfg.risk_pct if cfg is not None else None))
    if cfg is not None:
        ov = live_config.overrides_for(cfg, code, sid)
        if ov:
            params.update(ov)
            _LOGGER.info("[H57] %s×%s 应用配置覆盖: %s", code, sid, ov)
    return params


def _code_symbol(code: str) -> str:
    return f"{code}.SH" if code.startswith("68") else f"{code}.SZ"


def _risk(cfg=None) -> RiskManager:
    """风控配置：**源码默认** + `live_config.risk` 覆盖（H64）。

    2026-10-27（用户拍板 B）：单标的集中度 10% → 15%；
    2026-10-28（D2）：再定到 **20%**，并接入配置层（面板可改）。
    `cfg.risk` 只允许覆盖 `RiskConfig` 的既有字段，未知键直接忽略（不静默扩大面）。
    """
    base = dict(
        max_symbol_position_pct=0.20, max_account_position_pct=0.90,
        max_order_value_pct=0.10, max_daily_loss_pct=0.02,
        max_symbol_cumulative_loss_pct=0.05)
    if cfg is not None and getattr(cfg, "risk", None):
        for k, v in dict(cfg.risk).items():
            if k in RiskConfig.__dataclass_fields__:
                base[k] = v
            else:
                _LOGGER.warning("[H64] live_config.risk 含未知字段 %s（已忽略）", k)
    return RiskManager(RiskConfig(**base))


def _initial_cash(obs=None) -> float:
    """H64：资金基准 = `cfg.initial_cash`，无配置回落到 `INIT_CASH`。

    用 `getattr` 而非直接取属性：既有测试用 SimpleNamespace 造 obs，不带 cfg。
    """
    cfg = getattr(obs, "cfg", None)
    if cfg is not None and getattr(cfg, "initial_cash", None):
        return float(cfg.initial_cash)
    return float(INIT_CASH)


def _broker(day: str, cfg=None) -> PaperBroker:
    """纸面账户：初始资金取 `cfg.initial_cash`，无配置回落到 `INIT_CASH`（H64）。"""
    cash = float(cfg.initial_cash) if (cfg is not None and getattr(cfg, "initial_cash", None)) else INIT_CASH
    return PaperBroker(cash, day, risk_manager=_risk(cfg),
                       config=PaperBrokerConfig(slippage_bps=5, max_volume_participation=0.10))


@dataclass
class Observer:
    code: str
    strategy_id: str
    symbol: str
    params: dict
    engine: object = None
    broker: PaperBroker = None
    store: PaperStateStore = None
    cfg: object = None      # H64：live_config（None ⇒ 源码默认口径）
    # H47：去重的**权威状态**是"每个交易日已处理到的最高时刻"（体积 KB 级），
    # processed_times 只保留每日最高值，供 `max(...)` 这类既有调用继续可用。
    day_upto: dict = field(default_factory=dict)
    processed_times: set = field(default_factory=set)
    # H70：本观察单元的**资金基准**（配置值）。所有 cum_pnl / 回撤 / 峰值都以它为分母，
    # 不能再混用模块常量 INIT_CASH —— 那是"无配置时的兜底"，不是运行口径。
    initial_cash: float = INIT_CASH
    equity_high: float = INIT_CASH
    signals: List[dict] = field(default_factory=list)
    day_start_equity: float = INIT_CASH
    day_anchor_date: str = ""
    signal_path: Path = None
    day_high_equity: float = INIT_CASH
    max_drawdown_cumulative: float = 0.0

    def _is_processed(self, t) -> bool:
        """H47：bar 是否已处理 —— 按「当日已处理最高时刻」判定（与全量集合等价）。"""
        t = pd.Timestamp(t)
        cur = self.day_upto.get(t.date())
        return cur is not None and t <= cur

    def _mark_processed(self, t) -> None:
        t = pd.Timestamp(t)
        d = t.date()
        cur = self.day_upto.get(d)
        if cur is None or t > cur:
            self.day_upto[d] = t
        self.processed_times.add(t)   # 仅供 max(...) 类既有调用

    def watermark_path(self) -> Path:
        return self.store.root / "watermark.json"

    def load_watermark(self) -> None:
        p = self.watermark_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            # H47：优先读压缩后的按日水位；旧文件（21.9 万条列表）在此一次性派生，
            # 下次 save 时自动迁移成 KB 级，不需要人工改文件。
            upto = data.get("processed_upto_by_day") or {}
            self.day_upto = {pd.Timestamp(k).date(): pd.Timestamp(v) for k, v in upto.items()}
            legacy = pd.to_datetime(data.get("processed_times", []))
            for t in legacy:
                self._mark_processed(pd.Timestamp(t))
            self.processed_times = set(self.day_upto.values())
            self.equity_high = float(data.get("equity_high", self.initial_cash))
            self.day_start_equity = float(data.get("day_start_equity", self.initial_cash))
            self.day_anchor_date = str(data.get("day_anchor_date", ""))
            self.day_high_equity = float(data.get("day_high_equity", self.initial_cash))
            self.max_drawdown_cumulative = float(data.get("max_drawdown_cumulative", 0.0))

    def reset_daily_metrics(self, day: str) -> None:
        """按交易日期重置日内基准；同日重启必须保留，跨日才允许重置。"""
        if self.day_anchor_date == day:
            return
        equity = self.broker.account.equity() if self.broker is not None else self.initial_cash
        self.day_start_equity = equity
        self.day_high_equity = equity
        self.day_anchor_date = day

    def engine_state_path(self) -> Path:
        return self.store.root / "engine_state.json"

    def persist_all(self) -> None:
        """统一持久化：账户快照 + 水位线 + 引擎状态，避免多个入口漏掉其一。"""
        self.store.save(self.broker)
        self.save_watermark()
        self.save_engine_state()

    def save_engine_state(self) -> None:
        # H68(2026-10-28)：intraday_t 接入时漏了这条分支 —— 它的 state 挂在
        # `engine.engine.state`（IntradayTSharedEngine），与 grid 同构、与 ma_swing 不同。
        # 漏掉的后果：每次存快照都抛 AttributeError，引擎状态**从未落盘** ⇒
        # 重启后 buy_count_today / t_avg_cost / 冷却计时全部归零，等于每天重新开始计数。
        if self.strategy_id in ("grid_trade", "intraday_t", "sentiment_t"):
            state = self.engine.engine.state.to_dict()
        else:
            state = self.engine.state.to_dict()
        self.engine_state_path().write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def restore_engine_state(self) -> None:
        p = self.engine_state_path()
        if not p.exists():
            return
        state = json.loads(p.read_text(encoding="utf-8"))
        if self.strategy_id == "grid_trade":
            from core.grid_shared import GridSharedState
            self.engine.engine.state = GridSharedState.from_dict(state)
        elif self.strategy_id in ("intraday_t", "sentiment_t"):
            from core.intraday_t_shared import IntradayTSharedState
            self.engine.engine.state = IntradayTSharedState.from_dict(state)
        else:
            from core.ma_swing_live import MaSwingLiveState
            self.engine.state = MaSwingLiveState.from_dict(state)
            # 恢复后补 finalize：current_day 尚未进入 completed 的最后一天。
            finalize = getattr(self.engine, "finalize_if_dirty", None)
            if callable(finalize):
                finalize()

    def load_signals(self) -> None:
        if self.signal_path is not None and self.signal_path.exists():
            loaded = []
            for line in self.signal_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        loaded.append(json.loads(line))
                    except Exception:
                        continue
            self.signals = loaded

    def backfill_signals_from_fills(self) -> int:
        """从 broker.fills 重建完整信号日志，落盘 signals.jsonl，返回回填条数。

        目的：signals.jsonl 可能在首次建底仓时未落盘而丢失；fills 是持久化真相源，
        用它回填可保证信号日志与账户成交一致，信号对比不再缺块。
        """
        if self.signal_path is None or self.broker is None:
            return 0
        rows = []
        for f in self.broker.fills:
            rows.append({
                "signal_time": f.filled_at.isoformat(),
                "strategy_id": self.strategy_id,
                "symbol": f.symbol,
                "side": f.side.value if hasattr(f.side, "value") else str(f.side),
                "quantity": int(f.quantity),
                "reason": "(由fills回填)",
                "client_order_id": f.client_order_id,
                "order_status": "FILLED",
                "filled_quantity": int(f.quantity),
            })
        if rows:
            with self.signal_path.open("w", encoding="utf-8") as fh:
                for item in rows:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            self.signals = rows
        return len(rows)

    def save_watermark(self) -> None:
        p = self.watermark_path()
        p.write_text(json.dumps({
            "code": self.code,
            "strategy_id": self.strategy_id,
            "processed_upto_by_day": {d.isoformat(): t.isoformat()
                                      for d, t in sorted(self.day_upto.items())},
            "processed_times": [t.isoformat() for t in sorted(set(self.day_upto.values()))],
            "equity_high": self.equity_high,
            "day_start_equity": self.day_start_equity,
            "day_anchor_date": self.day_anchor_date,
            "day_high_equity": self.day_high_equity,
            "max_drawdown_cumulative": self.max_drawdown_cumulative,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def _engine_for(obs: Observer, df: pd.DataFrame):
    obs.params.setdefault("warmup_end", str(_trade_start_date(getattr(obs, "cfg", None)).date()))
    if obs.strategy_id == "grid_trade":
        return GridSharedLiveEngine(obs.symbol, obs.params, strategy_id=obs.strategy_id, df=df)
    if obs.strategy_id == "intraday_t":
        # H65：与 grid 同构（都吃 df 做 prepare），复权在引擎内部走 prepare_signal_prices
        return IntradayTSharedLiveEngine(obs.symbol, obs.params,
                                         strategy_id=obs.strategy_id, df=df)
    if obs.strategy_id == "sentiment_t":
        # H71：签名是 (symbol, params, df, strategy_id) —— 位置顺序与 intraday_t 不同，
        # 必须全部走关键字，否则会把 df 当 strategy_id 传进去。
        return SentimentTSharedLiveEngine(obs.symbol, obs.params,
                                          df=df, strategy_id=obs.strategy_id)
    prepared, events, _adj = prepare_signal_prices(df, obs.code)
    return MaSwingLiveEngine(obs.symbol, obs.params, strategy_id=obs.strategy_id,
                             prepared_df=prepared, corporate_action_dates=events)


def _load_live(code: str) -> pd.DataFrame:
    f = LIVE_DIR / f"stock_{code}_1m.csv"
    if not f.exists():
        return pd.DataFrame()
    return pd.read_csv(f, parse_dates=["time"]).reset_index(drop=True)


def _day_bar_count(code: str, day: str) -> int | None:
    """当日实际 1m bar 数（用于 H35 缺口标记）。取不到 ⇒ None（**不是 0**）。"""
    try:
        df = _load_live(code)
        if df is None or df.empty:
            return None
        return int((df["time"].dt.normalize() == pd.Timestamp(day)).sum())
    except Exception:  # noqa: BLE001 —— 计数失败不得影响摘要写出
        return None


def _position_cap_snapshot(obs: Observer) -> dict | None:
    """H46-③：底仓/权益 集中度快照（**只读**，不产生任何订单）。取不到 ⇒ None。"""
    try:
        acct = obs.broker.account
        positions = getattr(acct, "positions", None) or {}
        pos = positions.get(obs.symbol)
        if pos is None:
            return None
        shares = int(getattr(pos, "quantity", 0) or 0)
        price = float(getattr(pos, "market_price", 0) or 0)
        if shares <= 0 or price <= 0:
            return None
        equity = float(acct.equity())
        cash = float(getattr(acct, "cash", 0) or 0)
        # 上限显式取风控配置，避免 position_cap 的常量与 RiskConfig 各自漂移（"同源"注释不等于同源）
        cap = _risk(obs.cfg).config.max_symbol_position_pct
        return cap_evaluate(shares * price, equity, shares=shares, cash=cash, cap=cap)
    except Exception:  # noqa: BLE001 —— 监控失败不得影响摘要写出
        return None


def _build_observers() -> List[Observer]:
    observers = []
    cfg = live_config.load()
    combos = _resolve_combos(cfg)
    _record_applied_config(cfg, combos)
    for code, sid in combos:
        df = _load_live(code)
        if df.empty:
            raise RuntimeError(f"{code} 无 live 行情，无法启动")
        # 交易段：观察期统一起点（09-07）及之后；之前是预热段只喂指标。
        trade_df = df[df["time"] >= _trade_start_date(cfg)].reset_index(drop=True)
        if trade_df.empty:
            raise RuntimeError(f"{code} 无交易段数据（起点 {_trade_start_date(cfg).date()}）")
        first_price = float(trade_df["close"].iloc[0])
        params = _params_for(cfg, code, sid, first_price)
        symbol = _code_symbol(code)
        obs = Observer(code=code, strategy_id=sid, symbol=symbol, params=params, cfg=cfg)
        obs.store = PaperStateStore(OUTPUT_DIR / symbol / sid)
        obs.signal_path = OUTPUT_DIR / symbol / sid / "signals.jsonl"
        # H70-fix（2026-10-28）：资金基准必须在**两个分支之前**统一设置。
        # 此前只在「新建账户」分支设，走「恢复快照」分支时 initial_cash 仍是默认
        # INIT_CASH(100万) ⇒ cum_pnl = equity - 100万 = -698,166，
        # 而 day_pnl 用的是 30 万 ⇒ 同一天出现两个基准（当天实为 +1,834）。
        # 这个 bug 曾被"新建策略"掩盖过一次：新建分支设对了，旧策略没设。
        obs.initial_cash = _initial_cash(obs)

        # 优先恢复已有账户快照与水位线，实现重启续跑、不重复建仓。
        existing_broker = obs.store.load()
        if existing_broker is not None:
            obs.broker = existing_broker
            obs.load_watermark()
            obs.load_signals()
            # 引擎重建后先恢复引擎状态，再恢复账户状态。
            obs.engine = _engine_for(obs, df)
            obs.restore_engine_state()
        else:
            obs.broker = _broker(pd.Timestamp(trade_df["time"].iloc[0]).date().isoformat(), cfg)
            obs.engine = _engine_for(obs, df)
            obs.processed_times = set()
            # H69（2026-10-28）：**新建账户时交易段之前的 bar 属预热，必须标为已处理**。
            # 漏这一步的后果：`_process_new_bars` 的水位为空 ⇒ new_df = 全量 df
            # ⇒ 首次就把 12,240 根历史全当新数据回放。实测 intraday_t 在 08-11 就开仓，
            # 成交价 385.66 / 393.50 完全脱离当日 269~281 区间，成本与权益全部失真。
            _head = df[df["time"] < trade_df["time"].iloc[0]]
            if len(_head):
                for _d, _g in _head.groupby(_head["time"].dt.normalize().dt.date):
                    obs._mark_processed(pd.Timestamp(_g["time"].max()))
                _LOGGER.info("H69：%s×%s 预热段 %d 根已标为已处理（%s ~ %s）",
                             code, sid, len(_head),
                             _head["time"].iloc[0], _head["time"].iloc[-1])
            obs.initial_cash = _initial_cash(obs)
            obs.equity_high = obs.initial_cash
            obs.day_start_equity = obs.initial_cash
            obs.day_anchor_date = ""
            obs.day_high_equity = obs.initial_cash
            obs.max_drawdown_cumulative = 0.0
        obs.reset_daily_metrics(datetime.now().strftime("%Y-%m-%d"))
        observers.append(obs)
    return observers


def _with_price_limits(full_df: pd.DataFrame, new_df: pd.DataFrame,
                       symbol: str) -> pd.DataFrame:
    """H61：给待处理的 bar 补 `limit_up` / `limit_down`。

    前收必须取自**全量 df**：跨日时第一条 new bar 若只看 new_df，拿不到"上一交易日收盘"
    ⇒ 当日首根无限价 ⇒ `risk_manager` 的涨停禁买在当天第一根就失效。
    """
    if new_df.empty:
        return new_df
    try:
        priced = add_limit_columns(full_df, symbol, name=stock_name(symbol.split(".")[0]))
    except Exception:  # noqa: BLE001 —— 涨跌停算不出来不该阻断撮合
        _LOGGER.warning("H61：涨跌停列计算失败，本轮不启涨停保护", exc_info=True)
        return new_df
    limits = priced[["time", "limit_up", "limit_down"]].drop_duplicates("time")
    out = new_df.copy()
    out["time"] = pd.to_datetime(out["time"])
    return out.merge(limits, on="time", how="left")


def _process_new_bars(obs: Observer, df: pd.DataFrame,
                      ledger: "cross_risk.CrossSymbolLedger | None" = None) -> dict:
    if df.empty:
        return {"new_bars": 0}
    # 只处理未处理过的 bar。H47：按「当日最高时刻」判定 —— 与全量集合语义等价，
    # 但不需要把 3 年 × 240 根都留在内存/文件里。
    times = pd.to_datetime(df["time"])
    days = times.dt.normalize().dt.date
    upto = days.map(lambda d: obs.day_upto.get(d))
    new_df = df[~(upto.notna() & (times <= upto))].reset_index(drop=True)
    if len(new_df) and obs.day_upto:
        latest_day = max(obs.day_upto)
        old_days = new_df["time"].dt.normalize().dt.date
        if (old_days < latest_day).any():
            _LOGGER.warning("H47：检测到早于已处理水位的补录 bar（%s 条），按未处理处理",
                            int((old_days < latest_day).sum()))
    if new_df.empty:
        return {"new_bars": 0}
    # H61：补涨跌停列（前收取自全量 df）→ `paper_replay` 会自动读进 MarketSnapshot，
    # 从而让 `risk_manager.py` 的「涨停禁买」真正生效（此前 live 恒 None = 死保护）。
    new_df = _with_price_limits(df, new_df, obs.symbol)
    replay = OfflineReplayEngine(obs.broker, store=obs.store, gate=_gate_for(obs, ledger))
    replay.run(new_df, obs.symbol, obs.engine.on_bar, autosave_every=0)
    obs.broker = replay.broker
    for _, row in new_df.iterrows():
        obs._mark_processed(pd.Timestamp(row["time"]))
    obs.save_watermark()
    # 累积信号并落盘，供收盘后做信号对比。
    if obs.signal_path is not None and replay.signal_log:
        with obs.signal_path.open("a", encoding="utf-8") as fh:
            for item in replay.signal_log:
                item = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                        for k, v in item.items()}
                fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
        obs.signals.extend(replay.signal_log)
    return {"new_bars": len(new_df), "signals": len(replay.signal_log)}


def _obs_key(obs: Observer) -> str:
    return f"{obs.code}×{obs.strategy_id}"


def _gate_for(obs: Observer, ledger: "cross_risk.CrossSymbolLedger | None"):
    """H73：把「跨策略台账」包装成回放引擎要的 `gate(signal, price, own_shares) -> bool`。

    ledger 为 None（未配置 cross_symbol_cap）⇒ 返回 None ⇒ 回放不装闸，行为不变。
    """
    if ledger is None:
        return None
    key = _obs_key(obs)

    def _gate(signal, price, own_shares) -> bool:
        ok = ledger.check(signal.symbol, int(signal.quantity), float(price), key,
                          own_shares, strategy_id=obs.strategy_id,
                          at=str(getattr(signal, "signal_time", "")))
        if not ok:
            _LOGGER.warning("[H73] %s 买入 %s 股被跨策略闸门拒绝（合计超单标上限）",
                            key, signal.quantity)
        return ok

    return _gate


def _build_cross_ledger(observers: List[Observer]):
    """H73：按 `live_config.cross_symbol_cap` 建跨策略台账；未配置 ⇒ None。"""
    cfg = None
    for obs in observers:
        if getattr(obs, "cfg", None) is not None:
            cfg = obs.cfg
            break
    cap = float(getattr(cfg, "cross_symbol_cap", None) or 0) if cfg is not None else 0
    if not cap:
        return None
    base = float(getattr(cfg, "initial_cash", None) or 0)
    if base <= 0:
        return None
    ledger = cross_risk.CrossSymbolLedger(cap, base)
    for obs in observers:
        pos = obs.broker.account.positions.get(obs.symbol) if obs.broker is not None else None
        ledger.update(_obs_key(obs), obs.symbol,
                      int(getattr(pos, "quantity", 0) or 0) if pos is not None else 0)
    return ledger


def _run_once(observers: List[Observer], out) -> Dict[str, dict]:
    summary = {}
    # H73：整轮开始前给所有观察者建一次台账（未配置 ⇒ None ⇒ 不装闸）
    ledger = _build_cross_ledger(observers)
    for obs in observers:
        df = _load_live(obs.code)
        result = _process_new_bars(obs, df, ledger=ledger)
        if ledger is not None and obs.broker is not None:
            pos = obs.broker.account.positions.get(obs.symbol)
            ledger.update(_obs_key(obs), obs.symbol,
                          int(getattr(pos, "quantity", 0) or 0) if pos is not None else 0)
        summary[f"{obs.code}×{obs.strategy_id}"] = {
            "新bar": result.get("new_bars", 0),
            "累计订单": len(obs.broker.orders),
            "累计成交": len(obs.broker.fills),
            "账户权益": round(obs.broker.account.equity(), 2),
        }
    if ledger is not None and ledger.rejects:
        summary["跨策略闸门拒绝"] = {"笔数": len(ledger.rejects),
                                "明细": ledger.rejects[:10]}
    return summary


def _base_signals(obs: Observer) -> pd.DataFrame:
    """用当天 live 数据跑快速路径，生成基准信号（时间、方向、股数）。"""
    df = _load_live(obs.code)
    if df.empty:
        return pd.DataFrame(columns=["time", "direction", "shares"])
    trade_df = df[df["time"] >= _trade_start_date(getattr(obs, "cfg", None))].reset_index(drop=True)
    first_price = float(trade_df["close"].iloc[0])
    # H57-S3：**复用 obs.params**，不要重新 freeze。
    # 否则 live_config 的参数覆盖在"基准信号"这条路上丢失，
    # engine_consistency 就会拿错参数当参照物（§6.9 第 11 形态：机制存在但漏调用点）。
    if obs.params:
        params = {k: v for k, v in obs.params.items() if k != "warmup_end"}
    else:
        cls = _STRATEGY_CLASSES.get(obs.strategy_id)
        if cls is None:
            raise RuntimeError(f"未注册的 live 策略 {obs.strategy_id}")
        base_params = cls.default_params()
        params = freeze_params(obs.strategy_id, first_price, base_params)
    strategy = _STRATEGY_CLASSES[obs.strategy_id]()
    result = strategy.run(df, _initial_cash(obs), params,
                          context={"code": obs.code, "warmup_end": str(_trade_start_date(getattr(obs, "cfg", None)).date())})
    trades = result.trades.copy()
    trades["time"] = pd.to_datetime(trades["time"])
    return trades[["time", "direction", "shares"]]


def _compare_signals(obs: Observer) -> dict:
    """信号对比：引擎一致性 与 执行层损耗，两个数分开。"""
    live = []
    # 优先从真实成交(fills)重建 live 信号，避免 signals.jsonl 丢失导致"成交有、信号无"。
    # fills 持久化可靠，含 side/quantity/filled_at；引擎一致性只比 time+direction+shares。
    if obs.broker is not None and obs.broker.fills:
        for f in obs.broker.fills:
            live.append({
                "time": pd.Timestamp(f.filled_at),
                "direction": f.side.value if hasattr(f.side, "value") else str(f.side),
                "shares": int(f.quantity),
                "filled": int(f.quantity),
            })
    else:
        for s in obs.signals:
            live.append({
                "time": pd.Timestamp(s["signal_time"]),
                "direction": s["side"],
                "shares": int(s["quantity"]),
                "filled": int(s.get("filled_quantity") or 0),
            })
    live_df = pd.DataFrame(live)
    base_df = _base_signals(obs)

    engine_consistency = None
    execution_loss = None
    if len(base_df) and len(live_df):
        merged = base_df.merge(live_df, on=["time", "direction"], how="outer",
                               suffixes=("_base", "_live"), indicator=True)
        matched = int((merged["_merge"] == "both").sum())
        engine_consistency = matched / max(len(base_df), len(live_df))
        live_filled = live_df[live_df["filled"] > 0] if "filled" in live_df else live_df
        base_only = merged[merged["_merge"] == "left_only"]
        live_unfilled = merged[(merged["_merge"] == "right_only")]
        execution_loss = (len(base_only) + len(live_unfilled)) / max(len(base_df), 1)
    return {
        "engine_consistency": None if engine_consistency is None else round(engine_consistency, 6),
        "execution_loss": None if execution_loss is None else round(execution_loss, 6),
        "base_signals": len(base_df),
        "live_signals": len(live_df),
    }


def _upsert_daily_history(root: Path, payload: dict) -> None:
    """把当日摘要并入 daily_history.jsonl（同一天同策略幂等覆盖，按日期排序）。

    观察期需要跨日横向对比，单个 daily_summary.json 会被每日覆盖导致历史丢失，
    故用追加式台账累积；同日同策略重复写（重启/补跑）只保留最新一条，不产生重复。
    """
    hist = root / "daily_history.jsonl"
    rows: List[dict] = []
    if hist.exists():
        for line in hist.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (rec.get("date") == payload["date"]
                    and rec.get("strategy") == payload["strategy"]
                    and rec.get("code") == payload["code"]):
                continue  # 同日同策略旧记录由新记录替代
            rows.append(rec)
    rows.append(payload)
    rows.sort(key=lambda r: (r.get("date", ""), r.get("code", ""), r.get("strategy", "")))
    hist.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")


def _write_daily_summary(obs: Observer) -> dict:
    obs.backfill_signals_from_fills()
    # 均线波段预热段最后一天需显式 finalize，否则 completed 少一根日线。
    if obs.strategy_id == "ma_swing":
        finalize = getattr(obs.engine, "finalize_if_dirty", None)
        if callable(finalize):
            finalize()
    equity = obs.broker.account.equity()
    day_pnl = equity - obs.day_start_equity
    cum_pnl = equity - obs.initial_cash
    day_dd = 0.0
    if obs.day_high_equity > 0:
        day_dd = (equity - obs.day_high_equity) / obs.day_high_equity
    if obs.equity_high > 0:
        cum_dd = (equity - obs.equity_high) / obs.equity_high
    else:
        cum_dd = 0.0
    obs.max_drawdown_cumulative = min(obs.max_drawdown_cumulative, cum_dd)
    filled = sum(1 for o in obs.broker.orders.values()
                 if getattr(o, "filled_quantity", 0) > 0)
    rejected = sum(1 for o in obs.broker.orders.values()
                   if getattr(o, "status", None) is not None
                   and getattr(o.status, "value", "") == "REJECTED")
    payload = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "code": obs.code,
        "strategy": obs.strategy_id,
        "equity": round(equity, 2),
        "cash": round(obs.broker.account.cash, 2),
        "day_pnl": round(day_pnl, 2),
        "cum_pnl": round(cum_pnl, 2),
        "day_drawdown": round(day_dd, 6),
        "cumulative_drawdown": round(cum_dd, 6),
        "orders": len(obs.broker.orders),
        "fills": len(obs.broker.fills),
        "filled_orders": filled,
        "rejected_orders": rejected,
        "note": "绝对收益不与回测直接比，只比策略行为一致性",
        "signal_compare": _compare_signals(obs),
    }
    # H35 另一半：**数据不完整日必须带缺口标记**（缺口≥5% ⇒ void/作废）。
    # 否则 D11 那种 161/240 的日子会写出"看起来正常"的摘要，被误读成数据完整。
    payload["data_quality"] = day_bar_stats(_day_bar_count(obs.code, payload["date"]))
    # H46-③：底仓集中度（阈值与 RiskConfig.max_symbol_position_pct 同源 = 10%）。
    # ⚠️ 只监控与告警，**不减仓**（自动减仓需改 base_position 语义，属行为改动，另立）。
    cap = _position_cap_snapshot(obs)
    if cap is not None:
        # 分级（用户 2026-10-24 拍板）：超限 0~1pp ⇒ info（不推桌面），>1pp ⇒ alert。
        g = cap_grade(cap.get("ratio"), cap.get("cap"))
        cap["excess_pp"] = g["excess_pp"]
        cap["alert_level"] = g["level"]
        payload["position_cap"] = cap
        if g["exceeded"]:
            raise_alert(CAP_KIND,
                        f"{obs.code} 底仓/权益 {cap['ratio']:.2%} > {cap['cap']:.0%}"
                        f"（超 {g['excess_pp']:.2f}pp，超出 {cap['excess_value']:,.0f} 元）"
                        f"→ 仅告警，未减仓",
                        level=g["level"])
        else:
            clear_alert(CAP_KIND)
    # H46-③：把网格引擎的关键状态写进摘要。原摘要只有权益/订单计数，
    # 「已停止交易」与「正常运行但无信号」在外部完全无法区分（10-15~10-19 即如此漏掉）。
    # 纯新增观测字段，不参与任何交易决策。
    if obs.strategy_id == "grid_trade":
        # 注意：obs 可能没有 engine 属性（单测构造的轻量 Observer）⇒ 必须先取 obs 再取 engine。
        engine = getattr(obs, "engine", None)
        # 🚨 层级陷阱（2026-10-20 收盘后实测）：生产 obs.engine 是 GridSharedLiveEngine，
        #    其 state 与 _trend_defense_active 都在**内层** self.engine（GridSharedEngine）上；
        #    LiveEngine 自身既无 state 也无 _trend_defense_active（getattr 均返回 None）。
        #    单测用 SimpleNamespace 单层替身，测不出这个差异 ⇒ 曾导致四字段在生产上静默缺失。
        #    兼容两种形态：优先取内层，内层无 state 时回退外层。
        inner = getattr(engine, "engine", None)
        if getattr(inner, "state", None) is None:
            inner = engine
        st = getattr(inner, "state", None)
        if st is not None:
            defense_fn = getattr(inner, "_trend_defense_active", None)
            bp = getattr(st, "base_price", None)
            payload["stopped"] = bool(getattr(st, "stopped", False))
            payload["defense"] = bool(defense_fn()) if callable(defense_fn) else None
            payload["grid_bought"] = int(getattr(st, "grid_bought", 0))
            payload["base_price"] = None if bp is None else round(float(bp), 4)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    # 按天留档：跨日不再覆盖，观察期历史永久可回溯（09-09 曾因覆盖丢失两天摘要）
    (obs.store.root / f"daily_summary_{payload['date'].replace('-', '')}.json").write_text(
        body, encoding="utf-8")
    # 最新一天副本：保留 daily_summary.json，兼容既有引用（检查脚本 / 文档）
    (obs.store.root / "daily_summary.json").write_text(body, encoding="utf-8")
    # 追加台账：daily_history.jsonl，便于横向汇总与复盘
    _upsert_daily_history(obs.store.root, payload)
    obs.save_watermark()
    return payload


def _is_market_closed(observers: List[Observer]) -> bool:
    """判断是否已收盘：以 live 数据最新 bar 是否达到 14:59 为基准。"""
    for obs in observers:
        df = _load_live(obs.code)
        if not df.empty and df["time"].max().time() >= pd.Timestamp("14:59").time():
            return True
    return False


def main() -> None:
    observers = _build_observers()
    out = OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    while True:
        summary = _run_once(observers, out)
        for obs in observers:
            equity = obs.broker.account.equity()
            obs.day_high_equity = max(obs.day_high_equity, equity)
            obs.equity_high = max(obs.equity_high, equity)
            obs.persist_all()
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        if _is_market_closed(observers):
            print("== 已到收盘，写每日摘要 ==")
            for obs in observers:
                _write_daily_summary(obs)
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
