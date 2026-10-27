# -*- coding: utf-8 -*-
"""单标的策略基线（H61 配套）—— 用**与 live 完全相同的引擎与参数冻结逻辑**跑回测。

为什么要"同一引擎"：
  live 的 `engine_consistency` 就是把实盘成交回放结果和 `strategy.run()` 的基准信号比，
  所以基线必须走 `strategies/*.py` + `core/live_params.freeze_params`，
  不能拿父目录的 `backtest_generic.py`（那是另一套日内 T 引擎，口径不同）。

用法：
    python scripts/tool_strategy_baseline.py 301717 --start 2026-08-11
    python scripts/tool_strategy_baseline.py 301717 --override '{"ma_swing": {"slow_ma": 40}}'

注意：
  - `freeze_params` 的仓位按 **10%** 权益冻结（RISK_PCT=0.10），而风控上限是 **15%**
    （2026-10-27 拍板 B）⇒ 底仓 10% + 网格档位共用到 15%，两者不是一个数，别混。
  - 交易日不足时（如次新股 50 天），ma_swing 的慢线（默认 60）会全程 NaN ⇒ 0 信号，
    属参数与数据长度不匹配，不是策略失效；用 --override 降 slow_ma 做敏感性对照。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from core.live_params import INIT_CASH, freeze_params  # noqa: E402
from strategies.grid_trade import GridTradeStrategy  # noqa: E402
from strategies.ma_swing import MaSwingStrategy  # noqa: E402

_STRATS = {"grid_trade": GridTradeStrategy, "ma_swing": MaSwingStrategy}


def _load(code: str) -> pd.DataFrame:
    p = APP / "data" / f"stock_{code}_1m.csv"
    if not p.exists():
        raise SystemExit(f"缺少分钟数据：{p}")
    df = pd.read_csv(p, parse_dates=["time"])
    return df.sort_values("time").reset_index(drop=True)


def _fmt(x, nd=2):
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else str(x)


def main() -> int:
    ap = argparse.ArgumentParser(description="单标的策略基线（与 live 同引擎）")
    ap.add_argument("code")
    ap.add_argument("--cash", type=float, default=INIT_CASH)
    ap.add_argument("--start", default="", help="交易起点（同时也是 warmup_end），默认数据首日")
    ap.add_argument("--override", default="{}", help='参数覆盖 JSON，如 {"ma_swing":{"slow_ma":40}}')
    args = ap.parse_args()

    df = _load(args.code)
    overrides = json.loads(args.override or "{}")
    start = pd.Timestamp(args.start) if args.start else df["time"].iloc[0]

    trade = df[df["time"] >= start].reset_index(drop=True)
    if trade.empty:
        raise SystemExit("交易区间为空")
    first_price = float(trade["close"].iloc[0])
    days = trade["time"].dt.strftime("%Y-%m-%d").nunique()
    span = f"{trade['time'].iloc[0]:%Y-%m-%d}~{trade['time'].iloc[-1]:%Y-%m-%d}"

    print(f"标的 {args.code} | {span} | {days} 个交易日 | {len(trade)} 根 | 起始价 {first_price:.2f}")
    print(f"初始资金 {args.cash:,.0f}\n")

    rows = []
    for sid, cls in _STRATS.items():
        base = cls.default_params()
        if overrides.get(sid):
            base = {**base, **overrides[sid]}
        try:
            params = freeze_params(sid, first_price, base)
        except Exception as exc:  # noqa: BLE001
            print(f"[{sid}] 参数冻结失败：{exc}")
            continue
        res = cls().run(df, args.cash, params,
                        context={"code": args.code, "warmup_end": str(start.date())})
        trades = res.trades if res.trades is not None else pd.DataFrame()
        _dir = trades["direction"].astype(str).str.upper() if len(trades) else pd.Series(dtype=str)
        buys = int((_dir == "BUY").sum())
        sells = int((_dir == "SELL").sum())
        rows.append({
            "策略": sid,
            "总收益%": round(res.total_return * 100, 2),
            "年化%": round(res.annual_return * 100, 2),
            "最大回撤%": round(res.max_drawdown * 100, 2),
            "Calmar": round(res.calmar, 2) if res.calmar == res.calmar else None,
            "成交笔数": len(trades),
            "买/卖": f"{buys}/{sells}",
            "费用": round(res.total_fees, 2),
            "末权益": round(res.final_equity, 2),
            "关键参数": {k: params.get(k) for k in
                       ("base_position", "trade_shares", "grid_pct", "slow_ma", "position_pct")
                       if k in params},
        })

    for r in rows:
        print(f"[{r['策略']}] 总收益 {r['总收益%']}% | 年化 {r['年化%']}% | "
              f"最大回撤 {r['最大回撤%']}% | Calmar {r['Calmar']} | "
              f"成交 {r['成交笔数']} 笔（{r['买/卖']}）| 费用 {r['费用']} | 末权益 {r['末权益']:,.2f}")
        print(f"        参数: {r['关键参数']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
