"""费用口径核算 CLI（只读，不连柜台）。

输入两份 `tool_qmt_trader_probe.py` 的输出快照（同一交易日内、前后两次）：

    D:\\Anaconda3\\python.exe scripts/tool_fee_reconcile.py \
        --before test_outputs/qmt_trader_probe/probe_20261028_075759.json \
        --after  test_outputs/qmt_trader_probe/probe_20261028_085035.json

退出码：0 = 得到可用费用结论；2 = 证据不足/跨日/时钟可疑；1 = 输入错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import fee_reconcile  # noqa: E402


def _load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[ERROR] 文件不存在：{p}")
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="费用口径核算（只读）")
    ap.add_argument("--before", required=True, help="前一次探针快照 json")
    ap.add_argument("--after", required=True, help="后一次探针快照 json")
    ap.add_argument("--out", default=None, help="markdown 输出路径")
    args = ap.parse_args(argv)

    result = fee_reconcile.resolve_from_snapshots(_load(args.before), _load(args.after))
    text = fee_reconcile.render(result)
    print(text)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\n[写出] {args.out}")
    return 0 if result.get("status") in {"commission_field", "cash_delta"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
