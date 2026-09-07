# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.ma_swing_live import MaSwingLiveEngine
from core.paper_broker import PaperBroker, PaperBrokerConfig
from core.paper_replay import OfflineReplayEngine
from core.risk_manager import RiskConfig, RiskManager
from strategies.ma_swing import MaSwingStrategy

OUTPUT_DIR = APP_DIR / "test_outputs" / "ma_swing_live"


def broker(day):
    return PaperBroker(1_000_000, day, RiskManager(RiskConfig(
        max_symbol_position_pct=1.0, max_account_position_pct=1.0,
        max_order_value_pct=1.0, max_daily_loss_pct=1.0,
        max_symbol_cumulative_loss_pct=1.0)),
        PaperBrokerConfig(slippage_bps=5, max_volume_participation=1.0))


def collect(df):
    params = MaSwingStrategy.default_params()
    engine = MaSwingLiveEngine("300308.SZ", params)
    b = broker(pd.Timestamp(df.time.iloc[0]).date().isoformat())
    OfflineReplayEngine(b).run(df, "300308.SZ", engine.on_bar)
    return [(o.request.created_at, o.request.side.value, o.request.quantity)
            for o in b.orders.values()], b


def test_future_mutation_does_not_change_past_signals():
    df = pd.read_csv(APP_DIR / "data" / "stock_300308_1m.csv", parse_dates=["time"])
    cutoff = df["time"].sort_values().iloc[min(len(df) - 1, 50000)]
    prefix = df[df["time"] <= cutoff].copy()
    changed = df.copy()
    changed.loc[changed["time"] > cutoff, ["open", "high", "low", "close"]] *= 5
    original_signals, _ = collect(df)
    changed_signals, _ = collect(changed)
    assert [x for x in original_signals if x[0] <= cutoff] == [x for x in changed_signals if x[0] <= cutoff]


def test_no_duplicate_signal_per_day_and_original_run_unchanged():
    df = pd.read_csv(APP_DIR / "data" / "stock_300308_1m.csv", parse_dates=["time"])
    signals, b = collect(df)
    days = [(ts.date(), side) for ts, side, _ in signals]
    assert len(days) == len(set(days))
    result = MaSwingStrategy().run(df, 1_000_000, MaSwingStrategy.default_params(),
                                   context={"code": "300308"})
    assert result.final_equity > 0
    assert len(b.audit_log) >= len(signals)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    tests = [test_future_mutation_does_not_change_past_signals,
             test_no_duplicate_signal_per_day_and_original_run_unchanged]
    lines = []
    for test in tests:
        test()
        line = f"PASS {test.__name__}"
        print(line)
        lines.append(line)
    (OUTPUT_DIR / "ma_swing_live_test_result.txt").write_text(
        "\n".join(lines) + "\n严格增量均线波段测试通过。\n", encoding="utf-8")


if __name__ == "__main__":
    main()
