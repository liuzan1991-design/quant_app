# -*- coding: utf-8 -*-
"""大盘情绪做T严格增量双门禁：快速路径 vs 共享状态机+PaperBroker。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.sentiment_t_shared_live import SentimentTSharedLiveEngine  # noqa: E402
from core.paper_broker import PaperBroker, PaperBrokerConfig  # noqa: E402
from core.paper_replay import OfflineReplayEngine  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager  # noqa: E402
from strategies.sentiment_t import SentimentTStrategy  # noqa: E402

# ── 口径声明：本测试固定运行在「无情绪过滤」口径（H23 / A12 决策，2026-09-20）──
# 为什么显式强制：离线守卫会拦住情绪数据源，但"拦不拦得住"取决于
# `sentiment_data` 的缓存覆盖判据（`last >= end_dt.replace(hour=15, minute=0)`），
# 而该判据随数据文件的新鲜度变化 ⇒ **同一测试换个日期跑，口径会漂移**（H23 实测：
# 4 个标的中 300502 未降级 / 688256 完全降级 / 300308·600900 板块失效）。
# 这里把三个入口直接置空 ⇒ 恒降级 ⇒ **口径钉死、结果可复现**，且完全不依赖缓存状态。
# ⚠️ 代价：本测试**不再覆盖"板块过滤"逻辑** —— 该空窗登记为 **A13**。
import core.sentiment_data as _sd  # noqa: E402

_sd.load_index_min = lambda *a, **k: pd.DataFrame()
_sd.get_stock_industry_index = lambda *a, **k: None
_sd.load_industry_daily = lambda *a, **k: pd.DataFrame()

OUT = APP_DIR / "test_outputs" / "sentiment_t_dual_gate"
SIGNAL_GATE = 0.95
FILL_GATE = 0.90


def run(code):
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    symbol = f"{code}.SH" if code.startswith("68") else f"{code}.SZ"
    strategy = SentimentTStrategy()
    params = strategy.default_params()
    params["stop_loss_pct"] = {
        "low": params.pop("stop_loss_low"),
        "normal": params.pop("stop_loss_normal"),
        "high": params.pop("stop_loss_high"),
    }
    params.setdefault("min_bars", 10)
    params.setdefault("macd_fast", 5)
    params.setdefault("macd_slow", 10)
    params.setdefault("macd_signal", 3)
    params.setdefault("nine_turn_lookback", 3)
    params.setdefault("cooldown_sell_base", {"low": 3, "normal": 2, "high": 1})
    params.setdefault("cooldown_buy_base", {"low": 3, "normal": 5, "high": 7})
    params.setdefault("buy_back_gap_pct", 0.005)

    fast_params = strategy.default_params()
    fast = strategy.run(df, 1_000_000, fast_params, context={"code": code})

    risk = RiskManager(RiskConfig(max_symbol_position_pct=1, max_account_position_pct=1,
                                  max_order_value_pct=1, max_daily_loss_pct=1,
                                  max_symbol_cumulative_loss_pct=1))
    broker = PaperBroker(1_000_000, df.time.iloc[0].date().isoformat(), risk,
                         PaperBrokerConfig(
                             commission_rate=params["commission_rate"],
                             min_commission=params["min_commission"],
                             stamp_tax_rate=params["stamp_tax_rate"],
                             transfer_fee_rate=params["transfer_fee_rate"],
                             slippage_bps=params["slippage_bps"],
                             max_volume_participation=1))
    live = SentimentTSharedLiveEngine(symbol, params, df=df)
    replay = OfflineReplayEngine(broker)
    replay.run(df, symbol, live.on_bar)

    fast_signals = fast.trades[["time", "direction", "shares"]].copy()
    fast_signals["day"] = pd.to_datetime(fast_signals["time"]).dt.date
    live_signals = pd.DataFrame(replay.signal_log)
    live_signals["day"] = pd.to_datetime(live_signals["signal_time"]).dt.date
    live_signals = live_signals.rename(columns={"side": "direction"})
    fast_signals["seq"] = fast_signals.groupby(["day", "direction"]).cumcount()
    live_signals["seq"] = live_signals.groupby(["day", "direction"]).cumcount()
    compare = fast_signals.merge(live_signals, on=["day", "direction", "seq"],
                                 how="outer", suffixes=("_fast", "_live"), indicator=True)
    matched = int((compare["_merge"] == "both").sum())
    denominator = max(len(fast_signals), len(live_signals), 1)
    signal_rate = matched / denominator
    accepted = live_signals[live_signals["order_status"] != "REJECTED"]
    filled = accepted[accepted["filled_quantity"] > 0]
    fill_rate = len(filled) / max(len(accepted), 1)

    OUT.mkdir(parents=True, exist_ok=True)
    compare.to_csv(OUT / f"{code}_signal_compare.csv", index=False, encoding="utf-8-sig")
    live_signals.to_csv(OUT / f"{code}_raw_signals.csv", index=False, encoding="utf-8-sig")
    return {"股票": code, "快速信号": len(fast_signals), "增量信号": len(live_signals),
            "匹配": matched, "信号匹配率": signal_rate,
            "信号门禁": signal_rate >= SIGNAL_GATE,
            "接受订单": len(accepted), "成交订单": len(filled),
            "成交率": fill_rate, "成交门禁": fill_rate >= FILL_GATE}


def main():
    rows = [run(code) for code in ("300308", "300502", "688256")]
    result = pd.DataFrame(rows)
    result.to_csv(OUT / "dual_gate_summary.csv", index=False, encoding="utf-8-sig")
    print(result.to_string(index=False))
    failures = []
    for row in rows:
        if not row["信号门禁"]:
            failures.append(f'{row["股票"]}信号={row["信号匹配率"]:.2%}')
        if not row["成交门禁"]:
            failures.append(f'{row["股票"]}成交={row["成交率"]:.2%}')
    if failures:
        raise AssertionError("情绪做T双门禁未通过：" + ", ".join(failures))


if __name__ == "__main__":
    main()