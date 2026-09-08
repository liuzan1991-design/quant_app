# -*- coding: utf-8 -*-
"""实时增量撮合循环（观察期主程序）。

只读 `data/live/` 下采集进程落盘的最新 1 分钟行情，不碰 AmazingData SDK。
观察两个真增量组合：中际旭创×网格、中际旭创×均线波段。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

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
from core.paper_broker import PaperBroker, PaperBrokerConfig  # noqa: E402
from core.paper_replay import OfflineReplayEngine, StrategySignal  # noqa: E402
from core.paper_store import PaperStateStore  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager  # noqa: E402
from strategies.grid_trade import GridTradeStrategy  # noqa: E402
from strategies.ma_swing import MaSwingStrategy  # noqa: E402

LIVE_DIR = APP_DIR / "data" / "live"
OUTPUT_DIR = APP_DIR / "live_outputs"

# 观察期统一交易起点：底仓建仓价、信号、成交都从这天起；之前是预热段只喂指标。
TRADE_START_DATE = pd.Timestamp("2026-09-07")

COMBOS = [
    ("300308", "grid_trade"),
    ("300308", "ma_swing"),
]


def _code_symbol(code: str) -> str:
    return f"{code}.SH" if code.startswith("68") else f"{code}.SZ"


def _risk() -> RiskManager:
    return RiskManager(RiskConfig(
        max_symbol_position_pct=0.10, max_account_position_pct=0.90,
        max_order_value_pct=0.10, max_daily_loss_pct=0.02,
        max_symbol_cumulative_loss_pct=0.05))


def _broker(day: str) -> PaperBroker:
    return PaperBroker(INIT_CASH, day, risk_manager=_risk(),
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
    processed_times: set = field(default_factory=set)
    equity_high: float = INIT_CASH
    signals: List[dict] = field(default_factory=list)
    day_start_equity: float = INIT_CASH
    signal_path: Path = None
    day_high_equity: float = INIT_CASH
    max_drawdown_cumulative: float = 0.0

    def watermark_path(self) -> Path:
        return self.store.root / "watermark.json"

    def load_watermark(self) -> None:
        p = self.watermark_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            self.processed_times = set(pd.to_datetime(data.get("processed_times", [])))
            self.equity_high = float(data.get("equity_high", INIT_CASH))
            self.day_start_equity = float(data.get("day_start_equity", INIT_CASH))
            self.day_high_equity = float(data.get("day_high_equity", INIT_CASH))
            self.max_drawdown_cumulative = float(data.get("max_drawdown_cumulative", 0.0))

    def engine_state_path(self) -> Path:
        return self.store.root / "engine_state.json"

    def persist_all(self) -> None:
        """统一持久化：账户快照 + 水位线 + 引擎状态，避免多个入口漏掉其一。"""
        self.store.save(self.broker)
        self.save_watermark()
        self.save_engine_state()

    def save_engine_state(self) -> None:
        if self.strategy_id == "grid_trade":
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
            "processed_times": [t.isoformat() for t in sorted(self.processed_times)],
            "equity_high": self.equity_high,
            "day_start_equity": self.day_start_equity,
            "day_high_equity": self.day_high_equity,
            "max_drawdown_cumulative": self.max_drawdown_cumulative,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def _engine_for(obs: Observer, df: pd.DataFrame):
    obs.params.setdefault("warmup_end", str(TRADE_START_DATE.date()))
    if obs.strategy_id == "grid_trade":
        return GridSharedLiveEngine(obs.symbol, obs.params, strategy_id=obs.strategy_id, df=df)
    prepared, events, _adj = prepare_signal_prices(df, obs.code)
    return MaSwingLiveEngine(obs.symbol, obs.params, strategy_id=obs.strategy_id,
                             prepared_df=prepared, corporate_action_dates=events)


def _load_live(code: str) -> pd.DataFrame:
    f = LIVE_DIR / f"stock_{code}_1m.csv"
    if not f.exists():
        return pd.DataFrame()
    return pd.read_csv(f, parse_dates=["time"]).reset_index(drop=True)


def _build_observers() -> List[Observer]:
    observers = []
    for code, sid in COMBOS:
        df = _load_live(code)
        if df.empty:
            raise RuntimeError(f"{code} 无 live 行情，无法启动")
        # 交易段：观察期统一起点（09-07）及之后；之前是预热段只喂指标。
        trade_df = df[df["time"] >= TRADE_START_DATE].reset_index(drop=True)
        if trade_df.empty:
            raise RuntimeError(f"{code} 无交易段数据（起点 {TRADE_START_DATE.date()}）")
        first_price = float(trade_df["close"].iloc[0])
        base_params = (GridTradeStrategy.default_params() if sid == "grid_trade"
                       else MaSwingStrategy.default_params())
        params = freeze_params(sid, first_price, base_params)
        symbol = _code_symbol(code)
        obs = Observer(code=code, strategy_id=sid, symbol=symbol, params=params)
        obs.store = PaperStateStore(OUTPUT_DIR / symbol / sid)
        obs.signal_path = OUTPUT_DIR / symbol / sid / "signals.jsonl"

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
            obs.broker = _broker(pd.Timestamp(trade_df["time"].iloc[0]).date().isoformat())
            obs.engine = _engine_for(obs, df)
            obs.processed_times = set()
            obs.equity_high = INIT_CASH
            obs.day_start_equity = INIT_CASH
            obs.day_high_equity = INIT_CASH
            obs.max_drawdown_cumulative = 0.0
        observers.append(obs)
    return observers


def _process_new_bars(obs: Observer, df: pd.DataFrame) -> dict:
    if df.empty:
        return {"new_bars": 0}
    # 只处理未处理过的 bar
    new_df = df[~df["time"].isin(obs.processed_times)].reset_index(drop=True)
    if new_df.empty:
        return {"new_bars": 0}
    replay = OfflineReplayEngine(obs.broker, store=obs.store)
    replay.run(new_df, obs.symbol, obs.engine.on_bar, autosave_every=0)
    obs.broker = replay.broker
    for _, row in new_df.iterrows():
        obs.processed_times.add(pd.Timestamp(row["time"]))
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


def _run_once(observers: List[Observer], out) -> Dict[str, dict]:
    summary = {}
    for obs in observers:
        df = _load_live(obs.code)
        result = _process_new_bars(obs, df)
        summary[f"{obs.code}×{obs.strategy_id}"] = {
            "新bar": result.get("new_bars", 0),
            "累计订单": len(obs.broker.orders),
            "累计成交": len(obs.broker.fills),
            "账户权益": round(obs.broker.account.equity(), 2),
        }
    return summary


def _base_signals(obs: Observer) -> pd.DataFrame:
    """用当天 live 数据跑快速路径，生成基准信号（时间、方向、股数）。"""
    df = _load_live(obs.code)
    if df.empty:
        return pd.DataFrame(columns=["time", "direction", "shares"])
    trade_df = df[df["time"] >= TRADE_START_DATE].reset_index(drop=True)
    first_price = float(trade_df["close"].iloc[0])
    base_params = (GridTradeStrategy.default_params() if obs.strategy_id == "grid_trade"
                   else MaSwingStrategy.default_params())
    params = freeze_params(obs.strategy_id, first_price, base_params)
    strategy = (GridTradeStrategy() if obs.strategy_id == "grid_trade"
                else MaSwingStrategy())
    result = strategy.run(df, INIT_CASH, params,
                          context={"code": obs.code, "warmup_end": str(TRADE_START_DATE.date())})
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


def _write_daily_summary(obs: Observer) -> dict:
    obs.backfill_signals_from_fills()
    # 均线波段预热段最后一天需显式 finalize，否则 completed 少一根日线。
    if obs.strategy_id == "ma_swing":
        finalize = getattr(obs.engine, "finalize_if_dirty", None)
        if callable(finalize):
            finalize()
    equity = obs.broker.account.equity()
    day_pnl = equity - obs.day_start_equity
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
        "day_drawdown": round(day_dd, 6),
        "cumulative_drawdown": round(cum_dd, 6),
        "orders": len(obs.broker.orders),
        "fills": len(obs.broker.fills),
        "filled_orders": filled,
        "rejected_orders": rejected,
        "note": "绝对收益不与回测直接比，只比策略行为一致性",
        "signal_compare": _compare_signals(obs),
    }
    p = obs.store.root / "daily_summary.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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