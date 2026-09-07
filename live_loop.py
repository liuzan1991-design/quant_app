# -*- coding: utf-8 -*-
"""读盘撮合循环骨架（只读采集落盘结果，不碰任何行情 SDK）。

职责边界（依据决策 2/3）：
- 本循环只读 `data/live/` 下采集进程落盘的 1 分钟行情，绝不 import AmazingData、
  绝不登录。星耀单点登录态完全隔离在 `core/market_collector.py`。
- 用 1 分钟 bar 驱动三候选观察器（决策 4）：
    中际旭创 300308 × 移动网格
    中际旭创 300308 × 均线波段
    寒武纪   688256 × 大盘情绪做T
  每个组合一个独立 PaperBroker + 独立账户 + 独立观察器，互不串扰。
- 按「水位线」只处理上次之后的新 bar，逐根推进 on_bar -> PaperBroker 撮合
  -> PaperStateStore 持久化 + 每日对照落盘。

自测约定：
- 数据目录默认 `data/live/`；若为空则回退到历史数据目录（data/、backtest_data、
  D:/Codex输出/backtest_data），便于今天用历史数据离线走通全链路、不登录。
- 观察器的「实时预热/增量指标」是下一阶段工作，本骨架只搭清数据流与推进循环，
  相关占位以 TODO(实时预热) 标注，不在本阶段实现。

风险警示（留给周一验证后）：
- 盘中 bar 未收盘，on_bar 使用预计算字典时可能因新 bar 无条目而回退，
  需确认与回测口径一致后再正式观察。
"""
from __future__ import annotations

import json
import sys
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

from core.data import DATA_DIRS  # noqa: E402
from core.paper_broker import PaperBroker, PaperBrokerConfig  # noqa: E402
from core.paper_replay import OfflineReplayEngine  # noqa: E402
from core.paper_store import PaperStateStore  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager  # noqa: E402

LIVE_DIR = APP_DIR / "data" / "live"
OUTPUT_DIR = APP_DIR / "test_outputs" / "live_loop"

# 初始资金（沿用回测 100 万口径），仓位比例由 RiskManager 控制（决策 4：30% 口径待接入）。
INITIAL_CASH = 1_000_000.0


def code_symbol(code: str) -> str:
    """6 位代码 -> 带交易所后缀的 symbol（与 data_fetch.code_to_ad 同口径）。"""
    if code.startswith(("60", "68", "51", "56", "58", "50")):
        return f"{code}.SH"
    if code.startswith(("00", "30", "12", "15", "16", "18")):
        return f"{code}.SZ"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    return f"{code}.SH"


@dataclass
class Combination:
    """一个可观察组合：标的 + 策略 + 独立撮合账户 + 独立观察器。"""
    code: str
    strategy_id: str
    params: dict
    symbol: str = ""
    engine: object = None          # 观察器实例（GridSharedLiveEngine / MaSwingLiveEngine / SentimentTSharedLiveEngine）
    broker: PaperBroker = None
    store: PaperStateStore = None
    watermark: pd.Timestamp = None  # 已处理的最后一根 bar 时间（含），None 表示从头开始

    def __post_init__(self):
        if not self.symbol:
            self.symbol = code_symbol(self.code)


# 决策 4 定死的三个观察组合；参数取各自策略 default_params，待接入 30% 仓位口径。
def build_combinations() -> List[Combination]:
    from strategies.grid_trade import GridTradeStrategy
    from strategies.ma_swing import MaSwingStrategy

    risk = RiskManager(RiskConfig(
        max_symbol_position_pct=0.30,
        max_account_position_pct=0.90,
        max_order_value_pct=0.30,
        max_daily_loss_pct=0.02,
        max_symbol_cumulative_loss_pct=0.05,
    ))

    def _broker(day: str) -> PaperBroker:
        return PaperBroker(INITIAL_CASH, day, risk_manager=RiskManager(RiskConfig(
            max_symbol_position_pct=0.30, max_account_position_pct=0.90,
            max_order_value_pct=0.30, max_daily_loss_pct=0.02,
            max_symbol_cumulative_loss_pct=0.05)),
            config=PaperBrokerConfig(slippage_bps=5, max_volume_participation=0.10))

    return [
        Combination("300308", "grid_trade", GridTradeStrategy.default_params()),
        Combination("300308", "ma_swing", MaSwingStrategy.default_params()),
    ]


def _load_live_csv(code: str) -> Optional[pd.DataFrame]:
    """优先读 live 目录；为空则回退历史数据目录（自测用）。返回 reset_index 后的干净 df。"""
    live = LIVE_DIR / f"stock_{code}_1m.csv"
    if live.exists():
        df = pd.read_csv(live, parse_dates=["time"])
    else:
        from core.data import find_data_file
        f = find_data_file(code)
        if f is None:
            return None
        df = pd.read_csv(f, parse_dates=["time"])
    # 观察器 prepare 假定行索引从 0 连续（用位置 index 取预计算数组），必须 reset。
    return df.reset_index(drop=True)


def _build_engine(combo: Combination, df: pd.DataFrame):
    """按策略类型构造对应观察器，并完成历史数据预热。"""
    from core.adjustment import prepare_signal_prices
    from core.grid_shared_live import GridSharedLiveEngine
    from core.ma_swing_live import MaSwingLiveEngine
    from core.sentiment_t_shared_live import SentimentTSharedLiveEngine

    prepared, events, _adj = prepare_signal_prices(df, combo.code)
    if combo.strategy_id == "grid_trade":
        return GridSharedLiveEngine(combo.symbol, combo.params, strategy_id=combo.strategy_id, df=df)
    if combo.strategy_id == "ma_swing":
        return MaSwingLiveEngine(combo.symbol, combo.params, strategy_id=combo.strategy_id,
                                 prepared_df=prepared, corporate_action_dates=events)
    if combo.strategy_id == "sentiment_t":
        return SentimentTSharedLiveEngine(combo.symbol, combo.params, strategy_id=combo.strategy_id, df=df)
    raise ValueError(f"未知策略：{combo.strategy_id}")


class LiveMatchLoop:
    """读盘撮合主循环：给每个组合加载落盘行情，逐根新 bar 撮合并持久化。"""

    def __init__(self, combos: Optional[List[Combination]] = None,
                 output_dir: Optional[Path] = None):
        self.combos = combos or build_combinations()
        self.output_dir = Path(output_dir) if output_dir else OUTPUT_DIR
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def prepare(self, df_map: Dict[str, pd.DataFrame]) -> None:
        """为每个组合加载数据、构造观察器与撮合账户。"""
        for combo in self.combos:
            df = df_map.get(combo.code)
            if df is None or df.empty:
                raise ValueError(f"{combo.code} 无可用行情，无法预热观察器")
            first_day = pd.Timestamp(df["time"].iloc[0]).date().isoformat()
            combo.broker = PaperBroker(
                INITIAL_CASH, first_day,
                risk_manager=RiskManager(RiskConfig(
                    max_symbol_position_pct=0.30, max_account_position_pct=0.90,
                    max_order_value_pct=0.30, max_daily_loss_pct=0.02,
                    max_symbol_cumulative_loss_pct=0.05)),
                config=PaperBrokerConfig(slippage_bps=5, max_volume_participation=0.10))
            combo.engine = _build_engine(combo, df)
            combo.store = PaperStateStore(self.output_dir / combo.symbol / combo.strategy_id)
            combo.watermark = None

    def step(self, df_map: Dict[str, pd.DataFrame]) -> dict:
        """推进一轮：对每个组合，只处理水位之后的新 bar。返回本轮处理统计。"""
        summary = {}
        for combo in self.combos:
            df = df_map[combo.code]
            bars = OfflineReplayEngine.normalize_bars(df)  # 复用标准化：去重排序
            if combo.watermark is not None:
                bars = bars[bars["time"] > combo.watermark]
            if bars.empty:
                summary[f"{combo.code}×{combo.strategy_id}"] = {"本轮新增bar": 0}
                continue
            replay = OfflineReplayEngine(combo.broker, store=combo.store)
            replay.run(bars, combo.symbol, combo.engine.on_bar, autosave_every=0)
            combo.watermark = bars["time"].max()
            combo.broker = replay.broker
            summary[f"{combo.code}×{combo.strategy_id}"] = {
                "本轮新增bar": len(bars),
                "水位推进到": str(combo.watermark),
                "累计订单": len(combo.broker.orders),
                "累计成交": len(combo.broker.fills),
                "账户权益": round(combo.broker.account.equity(), 2),
            }
        return summary

    def close_day(self) -> dict:
        """每日收盘落盘：信号日志 + 账户快照 + 权益/回撤对照。"""
        out = {}
        for combo in self.combos:
            if combo.broker is None or combo.store is None:
                out[f"{combo.code}×{combo.strategy_id}"] = "未初始化"
                continue
            combo.store.save(combo.broker)
            symbol_dir = self.output_dir / combo.symbol
            name = symbol_dir / f"{combo.strategy_id}_daily_summary.json"
            payload = {
                "code": combo.code,
                "strategy": combo.strategy_id,
                "trading_day": combo.broker.account.trading_day,
                "equity": combo.broker.account.equity(),
                "cash": combo.broker.account.cash,
                "realized_pnl": combo.broker.account.realized_pnl_by_symbol,
                "orders": len(combo.broker.orders),
                "fills": len(combo.broker.fills),
                # TODO(决策6)：中际旭创×网格、寒武纪×情绪做T 单独回撤监控线在此落盘
                "daily_drawdown_monitoring": combo.strategy_id in ("grid_trade", "sentiment_t"),
            }
            name.parent.mkdir(parents=True, exist_ok=True)
            name.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")
            out[f"{combo.code}×{combo.strategy_id}"] = payload
        return out


def run_offline_selfcheck(recent_days: int = 60) -> None:
    """离线自测：用历史数据当 live 落盘，走通 读盘->预热->撮合->持久化 全链路（不登录）。

    recent_days：仅截取最近 N 个自然日数据，避免全量 20 万根 bar 预热/撮合超时，
    自测只验证链路，不等同于全量结果。
    """
    print("== 读盘撮合循环 离线自检（不登录）==")
    loop = LiveMatchLoop()
    df_map = {}
    for combo in loop.combos:
        df = _load_live_csv(combo.code)
        if df is None or df.empty:
            print(f"[warn] {combo.code} 无数据，跳过")
            continue
        df = df[df["time"] >= df["time"].max() - __import__("pandas").Timedelta(days=recent_days)]
        df = df.reset_index(drop=True)
        df_map[combo.code] = df
        print(f"[data] {combo.code}: {df['time'].min()} ~ {df['time'].max()} 共 {len(df)} 根")
    if not df_map:
        raise RuntimeError("无任何可用行情，自检终止")
    loop.prepare(df_map)
    summary = loop.step(df_map)
    for k, v in summary.items():
        print(f"[step] {v}")
    daily = loop.close_day()
    print(f"[daily] 已落盘每日对照：{list(daily.keys())}")
    print(f"[done] 输出目录：{loop.output_dir}")


if __name__ == "__main__":
    run_offline_selfcheck()