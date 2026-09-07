# -*- coding: utf-8 -*-
"""M4.1 严格增量信号一致性验证脚本。

用途：
1. 对比均线波段快速历史回测信号与严格增量 on_bar 信号。
2. 输出信号匹配率、成交率、差异明细和 Markdown 报告。
3. 作为进入模拟盘前的硬门槛证据；信号匹配率默认必须 >=95%，成交率 >=90%。

边界：
本脚本只做离线回放，不连接行情服务、不连接券商、不发送真实订单。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.adjustment import prepare_signal_prices  # noqa: E402
from core.ma_swing_live import MaSwingLiveEngine  # noqa: E402
from core.paper_broker import PaperBroker, PaperBrokerConfig  # noqa: E402
from core.paper_replay import OfflineReplayEngine  # noqa: E402
from core.risk_manager import RiskConfig, RiskManager  # noqa: E402
from strategies.ma_swing import MaSwingStrategy  # noqa: E402

DEFAULT_CODES = ("300308", "300502", "688256")
DEFAULT_OUTPUT = Path(r"D:\Codex输出\M4.1信号一致性")
DEFAULT_SIGNAL_GATE = 0.95
DEFAULT_FILL_GATE = 0.90


def symbol_of(code: str) -> str:
    code = code.split(".")[0]
    if code.startswith(("6", "68", "51", "56", "58", "50")):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"
    raise ValueError(f"无法识别股票代码：{code}")


def build_broker(df: pd.DataFrame, params: dict) -> PaperBroker:
    risk = RiskManager(RiskConfig(
        max_symbol_position_pct=1.0,
        max_account_position_pct=1.0,
        max_order_value_pct=1.0,
        max_daily_loss_pct=1.0,
        max_symbol_cumulative_loss_pct=1.0,
    ))
    config = PaperBrokerConfig(
        commission_rate=float(params["commission_rate"]),
        min_commission=float(params["min_commission"]),
        stamp_tax_rate=float(params["stamp_tax_rate"]),
        transfer_fee_rate=float(params["transfer_fee_rate"]),
        slippage_bps=float(params["slippage_bps"]),
        max_volume_participation=1.0,
    )
    first_day = pd.Timestamp(df["time"].iloc[0]).date().isoformat()
    return PaperBroker(1_000_000.0, first_day, risk, config)


def normalize_fast_signals(fast_trades: pd.DataFrame) -> pd.DataFrame:
    frame = fast_trades[["time", "direction", "shares", "reason"]].copy()
    frame["time"] = pd.to_datetime(frame["time"])
    frame["day"] = frame["time"].dt.date
    frame["seq"] = frame.groupby(["day", "direction"]).cumcount()
    frame["direction"] = frame["direction"].astype(str).str.upper()
    return frame


def normalize_live_signals(signal_log: Iterable[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(signal_log)
    if frame.empty:
        return pd.DataFrame(columns=[
            "time", "direction", "shares", "reason", "day", "seq",
            "order_status", "filled_quantity",
        ])
    frame = frame.rename(columns={
        "signal_time": "time", "side": "direction", "quantity": "shares",
    })
    frame["time"] = pd.to_datetime(frame["time"])
    frame["day"] = frame["time"].dt.date
    frame["direction"] = frame["direction"].astype(str).str.upper()
    frame["seq"] = frame.groupby(["day", "direction"]).cumcount()
    keep = ["time", "direction", "shares", "reason", "day", "seq",
            "order_status", "filled_quantity", "client_order_id"]
    return frame[[column for column in keep if column in frame.columns]]


def compare_signals(code: str, fast: pd.DataFrame, live: pd.DataFrame) -> pd.DataFrame:
    merged = fast.merge(
        live,
        on=["day", "direction", "seq"],
        how="outer",
        suffixes=("_fast", "_live"),
        indicator=True,
    )
    merged.insert(0, "股票代码", code)
    merged["匹配状态"] = merged["_merge"].map({
        "both": "匹配",
        "left_only": "仅快速路径",
        "right_only": "仅严格增量",
    })
    merged["时间差分钟"] = (
        pd.to_datetime(merged.get("time_live")) - pd.to_datetime(merged.get("time_fast"))
    ).dt.total_seconds() / 60
    merged["数量差"] = pd.to_numeric(
        merged.get("shares_live"), errors="coerce"
    ) - pd.to_numeric(merged.get("shares_fast"), errors="coerce")
    return merged.drop(columns=["_merge"])


def run_one(code: str, signal_gate: float, fill_gate: float) -> dict:
    data_path = APP_DIR / "data" / f"stock_{code}_1m.csv"
    if not data_path.is_file():
        raise FileNotFoundError(f"未找到本地分钟数据：{data_path}")

    df = pd.read_csv(data_path, parse_dates=["time"]).sort_values("time")
    symbol = symbol_of(code)
    params = MaSwingStrategy.default_params()

    fast_result = MaSwingStrategy().run(
        df.copy(), 1_000_000.0, params, context={"code": code}
    )
    fast = normalize_fast_signals(fast_result.trades)

    prepared, corporate_action_dates, adjustment = prepare_signal_prices(df, code)
    broker = build_broker(df, params)
    engine = MaSwingLiveEngine(
        symbol,
        params,
        prepared_df=prepared,
        corporate_action_dates=corporate_action_dates,
    )
    replay = OfflineReplayEngine(broker)
    replay.run(df, symbol, engine.on_bar)
    live = normalize_live_signals(replay.signal_log)

    detail = compare_signals(code, fast, live)
    matched = detail[detail["匹配状态"] == "匹配"]
    signal_denominator = max(len(fast), len(live), 1)
    signal_rate = len(matched) / signal_denominator

    accepted = live[live.get("order_status", pd.Series(dtype=str)) != "REJECTED"]
    filled = accepted[pd.to_numeric(accepted.get("filled_quantity"), errors="coerce").fillna(0) > 0]
    fill_rate = len(filled) / max(len(accepted), 1)

    quantity_equal = (
        pd.to_numeric(matched.get("shares_fast"), errors="coerce")
        == pd.to_numeric(matched.get("shares_live"), errors="coerce")
    )
    quantity_match_rate = float(quantity_equal.mean()) if len(matched) else 0.0
    same_time = matched["时间差分钟"].abs().fillna(float("inf")) <= 1.0
    time_match_rate = float(same_time.mean()) if len(matched) else 0.0

    return {
        "code": code,
        "symbol": symbol,
        "adjustment": adjustment,
        "bars": len(df),
        "trading_days": int(df["time"].dt.date.nunique()),
        "fast_signal_count": len(fast),
        "live_signal_count": len(live),
        "matched_count": len(matched),
        "only_fast_count": int((detail["匹配状态"] == "仅快速路径").sum()),
        "only_live_count": int((detail["匹配状态"] == "仅严格增量").sum()),
        "signal_match_rate": signal_rate,
        "signal_gate_passed": signal_rate >= signal_gate,
        "accepted_order_count": len(accepted),
        "filled_order_count": len(filled),
        "fill_rate": fill_rate,
        "fill_gate_passed": fill_rate >= fill_gate,
        "quantity_match_rate": quantity_match_rate,
        "time_match_rate_1min": time_match_rate,
        "fast_total_return": float(fast_result.total_return),
        "fast_max_drawdown": float(fast_result.max_drawdown),
        "detail": detail,
        "live_raw": live,
    }


def write_report(rows: list[dict], output_dir: Path,
                 signal_gate: float, fill_gate: float) -> Path:
    report_path = output_dir / "M4.1信号一致性报告.md"
    lines = [
        "# M4.1 信号一致性报告",
        "",
        "## 结论",
        "",
    ]
    all_signal_pass = all(row["signal_gate_passed"] for row in rows)
    all_fill_pass = all(row["fill_gate_passed"] for row in rows)
    if all_signal_pass and all_fill_pass:
        lines.append("**GO**：信号门禁和成交门禁全部通过，可以进入 M4.2 模拟盘任务框架。")
    else:
        lines.append("**NO-GO**：仍未达到进入模拟盘的信号一致性门槛。")

    lines.extend([
        "",
        f"- 信号门禁：≥{signal_gate:.0%}",
        f"- 成交门禁：≥{fill_gate:.0%}",
        "- 范围：均线波段快速历史回测 vs 严格增量 on_bar 引擎",
        "- 样本：中际旭创、新易盛、寒武纪",
        "- 模式：完全离线回放，不连接行情服务和券商",
        "",
        "## 汇总",
        "",
        "| 股票 | 交易日 | 快速信号 | 增量信号 | 匹配 | 仅快速 | 仅增量 | 信号匹配率 | 信号门禁 | 成交率 | 成交门禁 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ])
    for row in rows:
        lines.append(
            f"| {row['symbol']} | {row['trading_days']} | {row['fast_signal_count']} | "
            f"{row['live_signal_count']} | {row['matched_count']} | {row['only_fast_count']} | "
            f"{row['only_live_count']} | {row['signal_match_rate']:.2%} | "
            f"{'通过' if row['signal_gate_passed'] else '失败'} | "
            f"{row['fill_rate']:.2%} | {'通过' if row['fill_gate_passed'] else '失败'} |"
        )

    lines.extend([
        "",
        "## 匹配质量",
        "",
        "| 股票 | 数量一致率 | 1分钟内时间一致率 |",
        "|---|---:|---:|",
    ])
    for row in rows:
        lines.append(
            f"| {row['symbol']} | {row['quantity_match_rate']:.2%} | "
            f"{row['time_match_rate_1min']:.2%} |"
        )

    lines.extend([
        "",
        "## 差异解读",
        "",
    ])
    for row in rows:
        if row["signal_gate_passed"]:
            desc = "信号一致性达标。"
        else:
            desc = (
                f"信号未达标：仅快速 {row['only_fast_count']} 条，"
                f"仅严格增量 {row['only_live_count']} 条。"
            )
        lines.append(f"- {row['symbol']}：{desc}")

    lines.extend([
        "",
        "## 下一步",
        "",
        "1. 继续定位均线波段快速路径与严格增量路径的信号状态生命周期差异。",
        "2. 信号门禁全部达到 95% 后，再扩展到日内做T、网格和情绪做T。",
        "3. 在进入模拟盘前执行滑点、延迟、漏单、涨跌停和数据缺口压力测试。",
        "4. 当前结果不构成实盘依据，禁止连接真实交易账户。",
        "",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run M4.1 signal consistency validation")
    parser.add_argument("--codes", nargs="*", default=list(DEFAULT_CODES))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-gate", type=float, default=DEFAULT_SIGNAL_GATE)
    parser.add_argument("--fill-gate", type=float, default=DEFAULT_FILL_GATE)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for code in args.codes:
        result = run_one(code, args.signal_gate, args.fill_gate)
        detail = result.pop("detail")
        live_raw = result.pop("live_raw")
        detail.to_csv(
            args.output_dir / f"{code}_signal_consistency.csv",
            index=False,
            encoding="utf-8-sig",
        )
        live_raw.to_csv(
            args.output_dir / f"{code}_strict_live_signals.csv",
            index=False,
            encoding="utf-8-sig",
        )
        rows.append({"detail": detail, "live_raw": live_raw, **result})

    summary_rows = [{key: value for key, value in row.items()
                     if key not in {"detail", "live_raw"}} for row in rows]
    pd.DataFrame(summary_rows).to_csv(
        args.output_dir / "M4.1信号一致性汇总.csv",
        index=False,
        encoding="utf-8-sig",
    )
    payload = {
        "signal_gate": args.signal_gate,
        "fill_gate": args.fill_gate,
        "all_signal_gate_passed": all(row["signal_gate_passed"] for row in rows),
        "all_fill_gate_passed": all(row["fill_gate_passed"] for row in rows),
        "rows": summary_rows,
    }
    (args.output_dir / "M4.1信号一致性.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report_path = write_report(rows, args.output_dir, args.signal_gate, args.fill_gate)

    print(pd.DataFrame(summary_rows).to_string(index=False))
    print(f"报告：{report_path}")
    if not payload["all_signal_gate_passed"] or not payload["all_fill_gate_passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
