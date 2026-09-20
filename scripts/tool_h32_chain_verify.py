# -*- coding: utf-8 -*-
"""H32 关键推论验证：H31（缓存漂移）与 H32（时间穿越）是否**同一条因果链**？（只读）

推论
----
若 `compute_sector_status` 读的是**缓存末行**，那么：
  - 缓存被刷新（H31：追加了新的末尾交易日）⇒ 末行变了 ⇒ sector 标量可能变
  - 末行是未来（H32）⇒ 用未来判断过去

⇒ 两者**不是两个独立缺陷，而是同一根因的两副面孔**：
  H32 是"为什么结果会依赖缓存末行"，H31 是"缓存末行为什么会变"。

本脚本的判别实验
----------------
把缓存**尾部截掉若干行**（模拟"刷新前缓存更短"的状态），
看 sector 标量是否翻转。
- 若翻转 ⇒ **一次缓存刷新就足以改变历史回测结论** ⇒ H31/H32 同链成立。
- 若不翻转 ⇒ 两者独立，H31 另有来源。

同时检验一个**方法论盲点**：
A21 的「W1 验证段 == W2 训练段」40 对全等自洽校验，
**能否发现 H32？**（预期：不能 —— 因为两段读的是**同一个未来末行**，
两边一起错 ⇒ 自洽校验天然对"共享输入的缺陷"失明。）
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

# (行业, 回测区间末, 生产算出的 sector)
CASES = [
    ("801770.SI", "2023-12-29", "weak", "300308/2023"),
    ("801080.SI", "2024-12-31", "strong", "688256/2024"),
    ("801880.SI", "2023-12-29", "flat", "000572/2023"),
    ("801160.SI", "2023-12-29", "weak", "600900/2023"),
    ("801760.SI", "2023-12-29", "weak", "300364/2023"),
]


def main() -> None:
    print("=" * 100)
    print("【判别实验】截掉缓存尾部 N 行（模拟『刷新前缓存更短』）⇒ sector 是否翻转？")
    print("=" * 100)
    print("  逻辑：刷新只会**追加**新行。若删掉最近 N 行后 sector 变了，")
    print("        说明『某次刷新』本身就足以改变**历史窗口**的结论。\n")
    for ind, end, prod_sector, tag in CASES:
        f = DATA / f"industry_{ind.replace('.', '_')}_daily.csv"
        d = pd.read_csv(f, parse_dates=["time"]).sort_values("time").reset_index(drop=True)
        e = pd.Timestamp(end)
        sliced = d[d["time"] <= e]
        correct = compute_sector_status(sliced)
        print(f"  [{tag}] {ind}  回测区间末 {end}")
        print(f"      生产（全量末行 {d['time'].iloc[-1].date()}）: {prod_sector:<8}"
              f" | 应得（裁剪到区间末）: {correct}")
        flips = []
        for n in (1, 5, 20, 60, 120, 250):
            if len(d) - n < 20:
                break
            s = compute_sector_status(d.iloc[:-n])
            flips.append((n, s))
        line = "      " + "  ".join(f"截{n}行→{s}" for n, s in flips)
        print(line)
        changed = [n for n, s in flips if s != prod_sector]
        print(f"      ⇒ 尾部变化导致 sector 改变的行数档位: "
              f"{changed if changed else '无（该行业对尾部不敏感）'}\n")

    print("=" * 100)
    print("【方法论盲点】A21 的「W1 验证段 == W2 训练段」40 对全等自洽校验，能发现 H32 吗？")
    print("=" * 100)
    print("  W1 = 训练 2023 / 验证 2024；W2 = 训练 2024 / 验证 2025")
    print("  「W1 验证段」与「W2 训练段」都是 **2024 年**。")
    print()
    for ind, tag in [("801770.SI", "300308"), ("801080.SI", "688256"),
                     ("801880.SI", "000572"), ("801160.SI", "600900")]:
        f = DATA / f"industry_{ind.replace('.', '_')}_daily.csv"
        d = pd.read_csv(f, parse_dates=["time"]).sort_values("time").reset_index(drop=True)
        prod = compute_sector_status(d)                        # 两段都用这个
        corr = compute_sector_status(d[d["time"] <= pd.Timestamp("2024-12-31")])
        print(f"  {tag:<8} {ind}  2024 段：生产口径 sector = {prod:<8} "
              f"（W1验证段与W2训练段**都**读它 ⇒ 两边一致）")
        print(f"  {'':<8} 应得 = {corr:<8} ⇒ "
              f"{'⚠️ 生产口径下两段一起错，自洽校验**照样通过**' if prod != corr else '本例恰好一致'}")
    print()
    print("  ⇒ **结论**：该自洽校验检验的是「两段是否用了同一套输入」，")
    print("     而 H32 恰恰是**让两段共享同一个错误的未来末行** ⇒")
    print("     **自洽校验对 H32 结构性失明**"
          "（它只能发现「两段不一致」，发现不了「两段一起错」）。")


if __name__ == "__main__":
    main()
