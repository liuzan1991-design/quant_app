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
from core.ma_swing_signals import build_daily_signal_table, shifted_execution_signals
from reconcile_ma_swing import reconcile
from strategies.ma_swing import MaSwingStrategy

OUT = APP_DIR / "test_outputs" / "ma_swing_reconcile" / "first_divergence"


def serializable(value):
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def diagnose(code):
    merged, fast, live, broker = reconcile(code)
    diff = merged[merged["_merge"] != "both"].copy()
    if diff.empty:
        return {"股票": code, "状态": "无分叉"}
    day = min(diff["day"].dropna())
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    params = MaSwingStrategy.default_params()
    prepared, corporate_dates, adjustment = prepare_signal_prices(df, code)
    table = build_daily_signal_table(prepared, params)
    shifted = shifted_execution_signals(prepared, params)
    ts = pd.Timestamp(day)
    previous = table.index[table.index < ts][-1] if any(table.index < ts) else None
    report = {
        "股票": code,
        "第一处分叉日": str(day),
        "复权口径": adjustment,
        "是否公司行为日": day in set(corporate_dates),
        "快速当日交易": fast[fast["day"] == day].to_dict("records"),
        "增量当日交易": live[live["day"] == day].to_dict("records"),
        "前一完整日公共信号": ({k: serializable(v) for k, v in table.loc[previous].to_dict().items()}
                              if previous is not None else {}),
        "执行日移位信号": ({k: serializable(v) for k, v in shifted.loc[ts].to_dict().items()}
                          if ts in shifted.index else {}),
        "最终账户现金": broker.account.cash,
        "最终账户权益": broker.account.equity(),
    }
    return report


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    reports = [diagnose(code) for code in ["300308", "300502", "688256"]]
    (OUT / "first_divergence.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    rows = [{"股票": r["股票"], "第一处分叉日": r.get("第一处分叉日"),
             "快速交易": len(r.get("快速当日交易", [])),
             "增量交易": len(r.get("增量当日交易", [])),
             "公司行为日": r.get("是否公司行为日")}
            for r in reports]
    pd.DataFrame(rows).to_csv(OUT / "first_divergence_summary.csv",
                              index=False, encoding="utf-8-sig")
    print(pd.DataFrame(rows).to_string(index=False))


if __name__ == "__main__":
    main()
