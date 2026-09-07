# -*- coding: utf-8 -*-
"""建立日内做T快速路径黄金基准。

目的：锁定“截至当前bar累计VWAP”修复后的交易明细、权益曲线和关键指标。
后续共享状态机与严格增量路径必须与该基准对齐。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from strategies.intraday_t import IntradayTStrategy  # noqa: E402


OUT_DIR = Path(r"D:\Codex输出\日内做T黄金基准")
CODES = ("300308", "300502", "688256")


def frame_hash(frame: pd.DataFrame) -> str:
    normalized = frame.copy()
    for col in normalized.columns:
        if pd.api.types.is_datetime64_any_dtype(normalized[col]):
            normalized[col] = normalized[col].dt.strftime("%Y-%m-%d %H:%M:%S")
    text = normalized.to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    strategy = IntradayTStrategy()
    rows = []
    for code in CODES:
        df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
        params = strategy.default_params()
        result = strategy.run(df.copy(), 1_000_000.0, params, context={"code": code})
        run_dir = OUT_DIR / code
        run_dir.mkdir(parents=True, exist_ok=True)
        result.trades.to_csv(run_dir / "trades.csv", index=False, encoding="utf-8-sig")
        result.equity.to_csv(run_dir / "equity.csv", index=False, encoding="utf-8-sig")
        result.rejected.to_csv(run_dir / "rejected.csv", index=False, encoding="utf-8-sig")
        trades_hash = frame_hash(result.trades)
        equity_hash = frame_hash(result.equity)
        rejected_hash = frame_hash(result.rejected)
        manifest = {
            "code": code,
            "strategy_id": result.strategy_id,
            "initial_cash": result.init_cash,
            "start": str(df["time"].iloc[0]),
            "end": str(df["time"].iloc[-1]),
            "bars": len(df),
            "trading_days": int(df["time"].dt.date.nunique()),
            "trade_count": len(result.trades),
            "equity_rows": len(result.equity),
            "rejected_rows": len(result.rejected),
            "final_equity": result.final_equity,
            "total_return": result.total_return,
            "annual_return": result.annual_return,
            "max_drawdown": result.max_drawdown,
            "total_fees": result.total_fees,
            "trades_sha256": trades_hash,
            "equity_sha256": equity_hash,
            "rejected_sha256": rejected_hash,
            "vwap_pricing": "cumulative_to_current_bar_no_future_data",
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append(manifest)
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "golden_summary.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
