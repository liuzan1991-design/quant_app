# -*- coding: utf-8 -*-
"""四策略长周期、多阶段验证，并输出适配评分与中文报告。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
OUT_DIR = Path(r"D:\Codex输出")
sys.path.insert(0, str(APP_DIR))

from core.data import load_data, stock_name  # noqa: E402
from core.metrics import stock_return  # noqa: E402
from strategies import STRATEGIES, get_strategy  # noqa: E402


STOCKS = [
    ("688256", "高波动科技"), ("300308", "高波动科技"),
    ("300502", "高波动科技"), ("300418", "高波动传媒"),
    ("300364", "高波动传媒"), ("002371", "科技制造"),
    ("601619", "公用事业"), ("000572", "汽车"),
    ("600900", "公用事业"), ("002594", "汽车龙头"),
]
STRATEGY_IDS = ["intraday_t", "grid_trade", "sentiment_t", "ma_swing"]
PERIODS = [
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026YTD", "2026-01-01", "2026-08-21"),
    ("全周期", "2023-01-01", "2026-08-21"),
]


def _market_features(df: pd.DataFrame) -> dict:
    d = df.copy()
    d["date"] = pd.to_datetime(d["time"]).dt.normalize()
    daily = d.groupby("date").agg(open=("open", "first"), high=("high", "max"),
                                    low=("low", "min"), close=("close", "last"))
    ret = daily["close"].pct_change().dropna()
    amplitude = ((daily["high"] - daily["low"]) / daily["open"].replace(0, np.nan) * 100).dropna()
    ma20 = daily["close"].rolling(20).mean()
    above = (daily["close"] > ma20).dropna()
    return {
        "交易日": len(daily),
        "股票涨跌%": round(stock_return(df), 2),
        "年化波动%": round(float(ret.std() * np.sqrt(252) * 100), 2) if len(ret) else 0.0,
        "平均日振幅%": round(float(amplitude.mean()), 2) if len(amplitude) else 0.0,
        "MA20上方占比%": round(float(above.mean() * 100), 2) if len(above) else 0.0,
    }


def run_validation() -> pd.DataFrame:
    rows = []
    total = len(STOCKS) * len(PERIODS) * len(STRATEGY_IDS)
    done = 0
    for code, category in STOCKS:
        for period_name, start, end in PERIODS:
            df, source = load_data(code, start, end)
            if df.empty or pd.to_datetime(df["time"]).dt.date.nunique() < 40:
                continue
            feat = _market_features(df)
            for sid in STRATEGY_IDS:
                strategy = get_strategy(sid)
                try:
                    params = strategy.default_params()
                    init_cash = 1_000_000.0
                    # 统一用约30%初始仓位，使不同股价股票具有可比风险敞口；
                    # A股按100股整数手，资金不足100股的高价股跳过而非伪装成零回撤。
                    if sid == "ma_swing":
                        params["position_pct"] = 30.0
                    else:
                        first_price = float(df["close"].iloc[0])
                        base_shares = int((init_cash * 0.30 / first_price) // 100 * 100)
                        if base_shares < 100:
                            raise ValueError("100万元不足以按统一仓位买入100股")
                        params["base_position"] = base_shares
                        if "fixed_shares" in params:
                            params["fixed_shares"] = max(100, int(base_shares * 0.1 // 100 * 100))
                        if "trade_shares" in params:
                            params["trade_shares"] = max(100, int(base_shares * 0.1 // 100 * 100))
                        params["max_position"] = max(base_shares * 2, base_shares + 100)
                    result = strategy.run(df, init_cash, params, {"code": code})
                    rows.append({
                        "股票代码": code, "股票名称": stock_name(code), "股票类型": category,
                        "阶段": period_name, "开始": pd.Timestamp(df["time"].iloc[0]).date(),
                        "结束": pd.Timestamp(df["time"].iloc[-1]).date(), "策略ID": sid,
                        "策略": strategy.name, **feat,
                        "策略收益%": round(result.total_return * 100, 2),
                        "策略最大回撤%": round(result.max_drawdown * 100, 2),
                        "相对股票涨跌%": round(result.total_return * 100 - feat["股票涨跌%"], 2),
                        "交易笔数": len(result.trades), "手续费": round(result.total_fees, 2),
                        "数据源": str(source or ""), "错误": "",
                    })
                except Exception as exc:  # noqa: BLE001
                    rows.append({"股票代码": code, "股票名称": stock_name(code), "股票类型": category,
                                 "阶段": period_name, "策略ID": sid, "策略": strategy.name,
                                 "错误": str(exc)})
                done += 1
                print(f"[{done}/{total}] {stock_name(code)} {period_name} {strategy.name}")
    return pd.DataFrame(rows)


def build_scorecard(detail: pd.DataFrame) -> pd.DataFrame:
    yearly = detail[(detail["阶段"] != "全周期") & (detail["错误"] == "")].copy()
    rows = []
    for (code, name, sid, strategy), g in yearly.groupby(["股票代码", "股票名称", "策略ID", "策略"]):
        positive = float((g["策略收益%"] > 0).mean() * 100)
        beat = float((g["相对股票涨跌%"] > 0).mean() * 100)
        avg_ret = float(g["策略收益%"].mean())
        avg_dd = float(g["策略最大回撤%"].mean())
        worst_dd = float(g["策略最大回撤%"].min())
        # 兼顾绝对收益、跨阶段稳定性、跑赢持股与回撤；仅用于同批策略排序。
        score = (0.35 * np.clip(avg_ret, -30, 30) / 30 * 100
                 + 0.25 * positive + 0.20 * beat
                 + 0.20 * max(0.0, 100 + worst_dd * 3))
        if score >= 70 and worst_dd > -20 and positive >= 60:
            grade = "适配度高"
        elif score >= 52 and worst_dd > -35:
            grade = "可观察"
        else:
            grade = "暂不适合"
        rows.append({
            "股票代码": code, "股票名称": name, "策略ID": sid, "策略": strategy,
            "有效阶段数": len(g), "平均收益%": round(avg_ret, 2),
            "盈利阶段占比%": round(positive, 1), "跑赢股票占比%": round(beat, 1),
            "平均最大回撤%": round(avg_dd, 2), "最差最大回撤%": round(worst_dd, 2),
            "适配评分": round(float(score), 1), "结论": grade,
        })
    return pd.DataFrame(rows).sort_values(["股票代码", "适配评分"], ascending=[True, False])


def build_report(detail: pd.DataFrame, score: pd.DataFrame) -> str:
    valid = detail[detail["错误"] == ""].copy()
    full = valid[valid["阶段"] == "全周期"]
    lines = [
        "# 四策略长周期适配验证报告", "",
        "本报告使用本地星耀数智1分钟行情，统一初始资金100万元，费用按万一免五、卖出印花税千一、过户费万0.2；均线波段默认计5bps滑点。结果只验证历史适配性，不等于实盘承诺。", "",
        "## 验证范围", "",
        f"- 股票：{valid['股票代码'].nunique()}只；策略：{valid['策略ID'].nunique()}套；有效组合：{len(valid)}组。",
        f"- 时间：{valid['开始'].min()}至{valid['结束'].max()}，按年度/年内阶段和全周期分别计算。",
        "- 评分兼顾平均收益、盈利阶段比例、相对股票涨跌和最差回撤，只用于本批样本内横向排序。", "",
        "## 每只股票的优先策略", "",
    ]
    for code, g in score.groupby("股票代码", sort=False):
        best = g.iloc[0]
        runner = g.iloc[1] if len(g) > 1 else None
        line = (f"- {best['股票名称']}（{code}）：优先 **{best['策略']}**，评分{best['适配评分']}，"
                f"平均收益{best['平均收益%']:+.2f}%，最差回撤{best['最差最大回撤%']:.2f}%，结论“{best['结论']}”")
        if runner is not None:
            line += f"；备选 {runner['策略']}（{runner['适配评分']}分）"
        lines.append(line + "。")

    lines.extend(["", "## 各策略总体表现", ""])
    for strategy, g in score.groupby("策略"):
        lines.append(
            f"- {strategy}：平均评分{g['适配评分'].mean():.1f}，"
            f"高适配{(g['结论']=='适配度高').sum()}只，可观察{(g['结论']=='可观察').sum()}只，"
            f"样本最差回撤{g['最差最大回撤%'].min():.2f}%。")

    lines.extend(["", "## 全周期收益与风险", ""])
    for code, g in full.groupby("股票代码"):
        g = g.sort_values("策略收益%", ascending=False)
        stock = g.iloc[0]["股票名称"]
        sret = g.iloc[0]["股票涨跌%"]
        desc = "；".join(f"{r['策略']} {r['策略收益%']:+.2f}%/回撤{r['策略最大回撤%']:.2f}%"
                        for _, r in g.iterrows())
        lines.append(f"- {stock}（{code}），股票涨跌{sret:+.2f}%：{desc}。")

    lines.extend([
        "", "## 实盘门槛", "",
        "历史回测通过不等于可以自动实盘。建议仅把“适配度高”作为模拟盘候选，并同时满足：最近滚动一年仍为正、最差回撤可接受、至少20个交易日模拟盘成交结果稳定、板块映射与大盘数据可用。单只股票初始资金建议不超过总账户的10%至15%，网格和做T都必须设最大仓位。", "",
        "若股票已经进入MA20和MA60同步向下的下降趋势，四套策略都不应因为历史适配分高而继续开新仓。", "",
    ])
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    detail = run_validation()
    score = build_scorecard(detail)
    detail_path = OUT_DIR / "四策略长周期验证_明细.csv"
    score_path = OUT_DIR / "四策略股票适配评分.csv"
    report_path = OUT_DIR / "四策略长周期验证报告.md"
    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    score.to_csv(score_path, index=False, encoding="utf-8-sig")
    report_path.write_text(build_report(detail, score), encoding="utf-8")
    print(f"明细：{detail_path}")
    print(f"评分：{score_path}")
    print(f"报告：{report_path}")


if __name__ == "__main__":
    main()
