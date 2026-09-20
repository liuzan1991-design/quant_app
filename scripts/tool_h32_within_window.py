# -*- coding: utf-8 -*-
"""H32 第三层：裁剪**并不能**完全修好 —— 区间内的「期末标量决定全程」前视（只读）。

三层结构
--------
- **H32-a**（区间外穿越）：命中缓存返回未裁剪全量 ⇒ 用 2026-09 的末行判断 2023 年。
  **裁剪可修**（把 `end` 之后的行去掉）。
- **H32-b**（区间内前视）：即便裁剪，`compute_sector_status` 仍只返回**一个标量**，
  而 `sentiment_t.py:63-68` 用这一个标量改写**整段** `status_map`
  ⇒ 用「区间末的板块状态」决定「区间初的交易」。
  **裁剪修不了** —— 必须改语义（逐日滚动计算）。

本脚本量化 H32-b：区间内 sector 标量**实际翻转了多少次**。
若一次都没翻 ⇒ H32-b 只是理论问题；若翻了多次 ⇒ 它和 H32-a 同级。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core.sentiment_data import compute_sector_status  # noqa: E402

DATA = APP_DIR / "data"

CASES = [
    ("300308", "801770.SI", "2023-01-01", "2023-12-29"),
    ("300308", "801770.SI", "2023-01-01", "2026-08-21"),
    ("688256", "801080.SI", "2024-01-02", "2024-12-31"),
    ("300502", "801770.SI", "2023-01-01", "2023-12-29"),
    ("300418", "801760.SI", "2025-01-01", "2025-12-31"),
    ("600900", "801160.SI", "2024-01-02", "2024-12-31"),
]


def main() -> None:
    print("=" * 100)
    print("H32-b 量化：回测区间**内部**，板块状态标量翻转了几次？")
    print("=" * 100)
    print("  做法：对区间内每个交易日 t，用 [区间初, t] 的数据算一次 sector（真实可用口径），")
    print("        统计它在区间内变了多少次。\n")
    print(f"  {'标的':<8}{'行业':<12}{'区间':<26}{'区间内翻转次数':>14}{'出现过的状态'}")
    print("  " + "-" * 88)
    total_flips = 0
    for code, ind, start, end in CASES:
        f = DATA / f"industry_{ind.replace('.', '_')}_daily.csv"
        d = pd.read_csv(f, parse_dates=["time"]).sort_values("time").reset_index(drop=True)
        b, e = pd.Timestamp(start), pd.Timestamp(end)
        seg = d[(d["time"] >= b) & (d["time"] <= e)].reset_index(drop=True)
        if len(seg) < 20:
            print(f"  {code:<8}{ind:<12}{start}~{end:<12} 区间数据不足")
            continue
        states = []
        for i in range(19, len(seg)):
            states.append(compute_sector_status(seg.iloc[:i + 1]))
        flips = sum(1 for i in range(1, len(states)) if states[i] != states[i - 1])
        uniq = sorted(set(states))
        total_flips += flips
        print(f"  {code:<8}{ind:<12}{start}~{end:<12}{flips:>14}{'  ' + '/'.join(uniq)}")

    print()
    print("=" * 100)
    print("【结论】")
    print("=" * 100)
    print(f"  区间内板块状态共翻转 {total_flips} 次（6 个组合）")
    print()
    print("  ⇒ 而生产口径把整段压成**一个标量**（区间末值）⇒")
    print("     **区间内每一次翻转都被忽略**，全部按『期末状态』执行。")
    print()
    print("  三层严重度（从外到内）：")
    print("    ① H32-a 区间外穿越：用 2026-09 判断 2023 ⇒ 偏差最大，**裁剪可修**")
    print("    ② H32-b 区间内前视：用期末判断期初   ⇒ 裁剪**修不了**，需改语义")
    print("    ③ H31  缓存末行漂移：同一分析重跑数字不同 ⇒ **冻结可修**")
    print()
    print("  ⇒ **关键**：codex 提的『冻结缓存』只能修 ③；")
    print("     若只做冻结，得到的将是**确定地错**的结果（可复现，但仍是穿越的）。")


if __name__ == "__main__":
    main()
