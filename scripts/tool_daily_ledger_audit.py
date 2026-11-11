"""日终账本比对 CLI（只读）。

用法：
    D:\\Anaconda3\\python.exe scripts/tool_daily_ledger_audit.py \
        --symbol 300319.SZ --date 2026-11-11
    ... --strategy grid_trade      # 只跑一个策略
    ... --out D:\\Codex输出\\账本比对_20261111.md

退出码：0 = 全部一致；2 = 有阻断项；1 = 输入/读盘失败。
不写任何生产文件，不下单，不改配置。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import ledger_audit  # noqa: E402

DEFAULT_STRATEGIES = ("grid_trade", "ma_swing", "intraday_t", "sentiment_t")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="日终账本比对（只读）")
    ap.add_argument("--symbol", required=True, help="如 300319.SZ")
    ap.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    ap.add_argument("--strategy", action="append", default=None,
                    help="只跑指定策略，可重复；缺省跑四个")
    ap.add_argument("--live-dir", default=str(ROOT / "live_outputs"))
    ap.add_argument("--data-dir", default=str(ROOT / "data" / "live"))
    ap.add_argument("--out", default=None, help="输出 markdown 路径（缺省只打印）")
    args = ap.parse_args(argv)

    code = args.symbol.split(".")[0]
    csv_path = Path(args.data_dir) / f"stock_{code}_1m.csv"
    if not csv_path.exists():
        print(f"[ERROR] 行情文件不存在：{csv_path}", file=sys.stderr)
        return 1

    strategies = tuple(args.strategy) if args.strategy else DEFAULT_STRATEGIES
    base = Path(args.live_dir) / args.symbol
    reports: list[tuple[str, dict]] = []
    missing: list[str] = []
    for st in strategies:
        unit = base / st
        if not (unit / "paper_state.json").exists():
            missing.append(st)
            continue
        reports.append((st, ledger_audit.audit_unit(unit, csv_path, args.date, args.symbol)))

    if not reports:
        print(f"[ERROR] {base} 下没有可读的 paper_state.json（缺 {missing}）", file=sys.stderr)
        return 1

    blocks = []
    for name, r in reports:
        text = ledger_audit.render_markdown(r)
        print(f"\n### 策略：{name}")
        print(text)
        blocks.append(f"### 策略：{name}\n\n{text}")

    if args.out:
        payload = "\n\n---\n\n".join(blocks)
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"\n[写出] {args.out}")

    bad = [name for name, r in reports if r["verdict"] != "ok"]
    if bad:
        print(f"\n[结论] {len(bad)}/{len(reports)} 个策略不一致：{bad}", file=sys.stderr)
        return 2
    print(f"\n[结论] {len(reports)}/{len(reports)} 个策略一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
