# -*- coding: utf-8 -*-
"""四策略滚动窗口、样本外与滑点压力测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
OUT_DIR = Path(r"D:\Codex输出")
sys.path.insert(0, str(APP_DIR))

from core.data import load_data, stock_name  # noqa: E402
from strategies import get_strategy  # noqa: E402

STOCKS = ["688256", "300308", "300502", "300418", "300364",
          "002371", "600900", "601619", "002594", "000572"]
STRATEGIES = ["intraday_t", "grid_trade", "sentiment_t", "ma_swing"]


def normalized_params(sid: str, df: pd.DataFrame, cash: float, slip: int) -> dict:
    s = get_strategy(sid)
    p = s.default_params()
    p["slippage_bps"] = slip
    if sid == "ma_swing":
        p["position_pct"] = 30.0
    else:
        px = float(df["close"].iloc[0])
        shares = int((cash * 0.30 / px) // 100 * 100)
        if shares < 100:
            raise ValueError("资金不足100股")
        p["base_position"] = shares
        p["max_position"] = shares * 2
        if "fixed_shares" in p:
            p["fixed_shares"] = max(100, int(shares * 0.1 // 100 * 100))
        if "trade_shares" in p:
            p["trade_shares"] = max(100, int(shares * 0.1 // 100 * 100))
    return p


def main() -> None:
    cash = 1_000_000.0
    rows = []
    checkpoint = OUT_DIR / "四策略滚动与压力测试_检查点.csv"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for code in STOCKS:
        full, _ = load_data(code, "2023-01-01", "2026-08-21")
        if full.empty:
            continue
        first = pd.Timestamp(full["time"].min()).normalize()
        last = pd.Timestamp(full["time"].max()).normalize()
        starts = pd.date_range(first, last - pd.DateOffset(months=12), freq="6MS")
        windows = [(f"滚动12月_{s:%Y%m}", s, min(s + pd.DateOffset(months=12) - pd.Timedelta(days=1), last))
                   for s in starts]
        windows += [("样本外2025", pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31")),
                    ("样本外2026YTD", pd.Timestamp("2026-01-01"), last)]
        for label, begin, end in windows:
            df = full[(full["time"] >= begin) & (full["time"] < end + pd.Timedelta(days=1))].copy()
            df.attrs.update(full.attrs)
            if pd.to_datetime(df["time"]).dt.date.nunique() < 80:
                continue
            for sid in STRATEGIES:
                for slip in (5, 20):
                    try:
                        s = get_strategy(sid)
                        p = normalized_params(sid, df, cash, slip)
                        r = s.run(df, cash, p, {"code": code})
                        rows.append({"股票代码": code, "股票名称": stock_name(code),
                                     "窗口": label, "开始": begin.date(), "结束": end.date(),
                                     "策略ID": sid, "策略": s.name, "滑点bps": slip,
                                     "收益%": round(r.total_return * 100, 2),
                                     "最大回撤%": round(r.max_drawdown * 100, 2),
                                     "平均资金占用%": round(r.avg_exposure * 100, 1),
                                     "占用资金收益%": round(r.capital_return * 100, 2),
                                     "交易笔数": len(r.trades)})
                    except Exception as exc:  # noqa: BLE001
                        rows.append({"股票代码": code, "股票名称": stock_name(code),
                                     "窗口": label, "策略ID": sid, "滑点bps": slip,
                                     "错误": str(exc)})
            print(code, label, "完成")
        pd.DataFrame(rows).to_csv(checkpoint, index=False, encoding="utf-8-sig")

    detail = pd.DataFrame(rows)
    valid = detail.copy() if "错误" not in detail.columns else detail[detail["错误"].fillna("") == ""].copy()
    base = valid[valid["滑点bps"] == 5]
    stress = valid[valid["滑点bps"] == 20]
    summary = base.groupby(["股票代码", "股票名称", "策略ID", "策略"]).agg(
        窗口数=("窗口", "count"), 平均收益=("收益%", "mean"),
        中位收益=("收益%", "median"), 盈利窗口比例=("收益%", lambda x: (x > 0).mean() * 100),
        最差收益=("收益%", "min"), 最差回撤=("最大回撤%", "min"),
        平均资金占用=("平均资金占用%", "mean"), 平均占用资金收益=("占用资金收益%", "mean"),
    ).reset_index()
    stress_sum = stress.groupby(["股票代码", "策略ID"]).agg(
        压力平均收益=("收益%", "mean"), 压力盈利窗口比例=("收益%", lambda x: (x > 0).mean() * 100)
    ).reset_index()
    summary = summary.merge(stress_sum, on=["股票代码", "策略ID"], how="left")
    summary["模拟盘候选"] = np.where(
        (summary["盈利窗口比例"] >= 60) & (summary["压力盈利窗口比例"] >= 50) &
        (summary["平均收益"] > 0) & (summary["最差回撤"] > -25), "是", "否")
    summary = summary.sort_values(["模拟盘候选", "盈利窗口比例", "平均收益"], ascending=[False, False, False])

    detail.to_csv(OUT_DIR / "四策略滚动与压力测试_明细.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "四策略滚动与压力测试_汇总.csv", index=False, encoding="utf-8-sig")
    candidates = summary[summary["模拟盘候选"] == "是"]
    lines = ["# 四策略滚动窗口与压力测试", "",
             "口径：12个月窗口、每3个月滚动；另含2025与2026YTD样本外窗口；统一约30%风险仓位；正常滑点5bps，压力滑点20bps。", "",
             "## 模拟盘候选", ""]
    if candidates.empty:
        lines.append("没有组合同时通过盈利窗口、压力滑点和回撤门槛。")
    else:
        for _, r in candidates.iterrows():
            lines.append(f"- {r['股票名称']}（{r['股票代码']}）× {r['策略']}："
                         f"盈利窗口{r['盈利窗口比例']:.0f}%，平均收益{r['平均收益']:+.2f}%，"
                         f"最差回撤{r['最差回撤']:.2f}%，20bps压力盈利窗口{r['压力盈利窗口比例']:.0f}%。")
    lines += ["", "仅列为模拟盘候选，不代表可以直接自动实盘。"]
    (OUT_DIR / "四策略滚动与压力测试报告.md").write_text("\n".join(lines), encoding="utf-8")
    print("完成", len(valid), "组；候选", len(candidates), "组")


if __name__ == "__main__":
    main()
