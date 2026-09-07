# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.adjustment import prepare_signal_prices
from core.ma_swing_signals import shifted_execution_signals
from reconcile_ma_swing import reconcile
from strategies.ma_swing import MaSwingStrategy

OUT = APP_DIR / "test_outputs" / "ma_swing_reconcile" / "second_divergence"


def clean(value):
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def diagnose(code):
    merged, fast, live, broker = reconcile(code)
    diff = merged[merged["_merge"] != "both"].copy()
    diff = diff[pd.to_datetime(diff["day"]) > pd.Timestamp("2023-01-03")]
    day = min(diff["day"].dropna())
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    params = MaSwingStrategy.default_params()
    prepared, corporate_dates, adjustment = prepare_signal_prices(df, code)
    signals = shifted_execution_signals(prepared, params)
    ts = pd.Timestamp(day)
    signal = ({k: clean(v) for k, v in signals.loc[ts].to_dict().items()}
              if ts in signals.index else {})
    return {
        "股票": code,
        "第二处分叉日": str(day),
        "复权口径": adjustment,
        "公司行为日": day in set(corporate_dates),
        "公共执行日信号": signal,
        "快速当日交易": fast[fast["day"] == day].to_dict("records"),
        "增量当日交易": live[live["day"] == day].to_dict("records"),
        "最终现金": broker.account.cash,
        "最终权益": broker.account.equity(),
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    reports = [diagnose(code) for code in ["300308", "300502", "688256"]]
    (OUT / "second_divergence.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    rows = [{"股票": r["股票"], "第二处分叉日": r["第二处分叉日"],
             "买入信号": r["公共执行日信号"].get("buy_signal"),
             "趋势退出": r["公共执行日信号"].get("trend_exit"),
             "快速交易数": len(r["快速当日交易"]),
             "增量交易数": len(r["增量当日交易"])} for r in reports]
    pd.DataFrame(rows).to_csv(OUT / "second_divergence_summary.csv",
                              index=False, encoding="utf-8-sig")
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
