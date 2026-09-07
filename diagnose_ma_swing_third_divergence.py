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

OUT = APP_DIR / "test_outputs" / "ma_swing_reconcile" / "third_divergence"


def value(v):
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def diagnose(code):
    merged, fast, live, broker = reconcile(code)
    diff = merged[(merged["_merge"] != "both") &
                  (pd.to_datetime(merged["day"]) > pd.Timestamp("2023-04-04"))]
    day = min(diff["day"].dropna())
    df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
    prepared, events, adjustment = prepare_signal_prices(df, code)
    params = MaSwingStrategy.default_params()
    sigs = shifted_execution_signals(prepared, params)
    ts = pd.Timestamp(day)
    sig = {k: value(v) for k, v in sigs.loc[ts].to_dict().items()} if ts in sigs.index else {}
    day_bars = df[pd.to_datetime(df["time"]).dt.date == day]
    open_price = float(day_bars.iloc[0]["open"]) if len(day_bars) else None
    high_price = float(day_bars["high"].max()) if len(day_bars) else None
    atr_real = None
    if sig.get("atr") is not None and ts in prepared.set_index("time").index and open_price:
        signal_open = float(prepared.set_index("time").loc[day_bars.iloc[0]["time"], "signal_open"])
        atr_real = float(sig["atr"]) * open_price / signal_open if signal_open else None
    return {
        "股票": code, "第三处分叉日": str(day), "复权口径": adjustment,
        "公司行为日": day in set(events), "公共信号": sig,
        "当日开盘": open_price, "当日最高": high_price,
        "ATR真实价尺度": atr_real,
        "快速当日交易": fast[fast["day"] == day].to_dict("records"),
        "增量当日交易": live[live["day"] == day].to_dict("records"),
        "最终现金": broker.account.cash, "最终权益": broker.account.equity(),
        "增量订单状态": [{"id": o.request.client_order_id, "时间": str(o.request.created_at),
                          "方向": o.request.side.value, "状态": o.status.value,
                          "数量": o.request.quantity, "成交": o.filled_quantity}
                         for o in broker.orders.values()
                         if o.request.created_at.date() == day],
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    reports = [diagnose(c) for c in ["300308", "300502", "688256"]]
    (OUT / "third_divergence.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    summary = pd.DataFrame([{"股票": r["股票"], "第三处分叉日": r["第三处分叉日"],
                             "开盘": r["当日开盘"], "最高": r["当日最高"],
                             "ATR": r["ATR真实价尺度"],
                             "快速交易": len(r["快速当日交易"]),
                             "增量交易": len(r["增量当日交易"])} for r in reports])
    summary.to_csv(OUT / "third_divergence_summary.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
