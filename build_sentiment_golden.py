# -*- coding: utf-8 -*-
"""建立大盘情绪做T快速路径黄金基准（修复未来VWAP后的新口径）。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
for _p in (str(APP_DIR), str(PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from strategies.sentiment_t import SentimentTStrategy  # noqa: E402


OUT_DIR = Path(r"D:\Codex输出\情绪做T黄金基准")
CODES = ("300308", "300502", "688256", "600900")


def frame_hash(frame: pd.DataFrame) -> str:
    normalized = frame.copy()
    for col in normalized.columns:
        if pd.api.types.is_datetime64_any_dtype(normalized[col]):
            normalized[col] = normalized[col].dt.strftime("%Y-%m-%d %H:%M:%S")
    text = normalized.to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    strategy = SentimentTStrategy()
    rows = []
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="仅补跑指定代码，如 --only 600900")
    args = parser.parse_args()
    codes = (args.only,) if args.only else CODES
    for code in codes:
        df = pd.read_csv(APP_DIR / "data" / f"stock_{code}_1m.csv", parse_dates=["time"])
        params = strategy.default_params()
        result = strategy.run(df.copy(), 1_000_000.0, params, context={"code": code})
        run_dir = OUT_DIR / code
        run_dir.mkdir(parents=True, exist_ok=True)
        result.trades.to_csv(run_dir / "trades.csv", index=False, encoding="utf-8-sig")
        result.equity.to_csv(run_dir / "equity.csv", index=False, encoding="utf-8-sig")
        result.rejected.to_csv(run_dir / "rejected.csv", index=False, encoding="utf-8-sig")
        manifest = {
            "code": code,
            "strategy_id": result.strategy_id,
            "initial_cash": result.init_cash,
            "bars": len(df),
            "trading_days": int(df["time"].dt.date.nunique()),
            "trade_count": len(result.trades),
            "rejected_count": len(result.rejected),
            "final_equity": result.final_equity,
            "total_return": result.total_return,
            "max_drawdown": result.max_drawdown,
            "total_fees": result.total_fees,
            "trades_sha256": frame_hash(result.trades),
            "equity_sha256": frame_hash(result.equity),
            "rejected_sha256": frame_hash(result.rejected),
            "vwap_pricing": "fixed_cumulative_to_current_bar_no_future_data",
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append(manifest)
    summary = pd.DataFrame(rows)
    summary.to_csv(OUT_DIR / "golden_summary.csv", index=False, encoding="utf-8-sig")
    print(summary[["code", "trade_count", "rejected_count", "total_return", "max_drawdown"]].to_string(index=False))


if __name__ == "__main__":
    main()