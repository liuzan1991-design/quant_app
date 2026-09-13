# -*- coding: utf-8 -*-
"""脚本式测试的 smoke runner（P2-②）。

背景
----
项目里有一批"脚本式"测试——没有 `test_` 函数，靠 `if __name__ == "__main__"` 跑，
pytest 收不到；但**它们已有非零退出判据**，外部可以判定成败：

- 3 个 `*_shared_consistency.py`：失败 `raise SystemExit(2)`
- 5 个 `*_dual_gate.py` / `*_consistency_gate.py`：失败 `raise AssertionError`（退出码 1）

本文件用 subprocess 把它们纳入 pytest 回归，断言退出码为 0。

分档
----
- 一致性测试：`slow` + `external`
  （依赖 `D:\\Codex输出` 下的黄金基准目录；逐 bar 全量回放，分钟级）
- 门禁测试：`slow`
  （用项目内本地 CSV，但同样要跑多标的 × 多策略全量回放——**实测 2 个脚本 5 分 40 秒未完成**，故同属慢档）

> 因此"日常档"（`-m "not slow and not external"`）**只含 A 类 19 个快速用例**。
> 凡是改动核心引擎（策略 / shared 状态机 / 撮合），**必须跑慢档**：
> `pytest -m "not external"`（含门禁）；涉及黄金基准时再跑 `pytest`（全量）。

安全边界
--------
**不包含任何会 `ad.login()` 的脚本**（`test_fetch.py` / `test_industry.py` /
`test_sentiment_data.py` / `test_down_market.py`）——它们会与观察期 `live_run.py`
抢 AmazingData 单点登录，一旦执行会踢掉当天采集。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]

# 一致性测试：慢 + 依赖外部黄金基准
CONSISTENCY = [
    "test_grid_shared_consistency.py",
    "test_intraday_t_shared_consistency.py",
    "test_sentiment_t_shared_consistency.py",
]

# 门禁测试：用项目内本地 CSV
GATES = [
    "test_grid_dual_gate.py",
    "test_intraday_t_dual_gate.py",
    "test_ma_swing_dual_gate.py",
    "test_sentiment_t_dual_gate.py",
    "test_ma_swing_consistency_gate.py",
]


def _run_script(name: str, timeout: int) -> None:
    """跑脚本并断言退出码为 0（判据在脚本内部）。"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(APP / name)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(APP), timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{name} 退出码={result.returncode}（0 才算通过）\n"
        f"--- stdout 尾部 ---\n{(result.stdout or '')[-1500:]}\n"
        f"--- stderr 尾部 ---\n{(result.stderr or '')[-1500:]}"
    )


@pytest.mark.slow
@pytest.mark.external
@pytest.mark.parametrize("name", CONSISTENCY)
def test_consistency_script(name: str) -> None:
    """引擎一致性验证（分钟级，依赖外部黄金基准）。"""
    _run_script(name, timeout=1800)


@pytest.mark.slow
@pytest.mark.parametrize("name", GATES)
def test_gate_script(name: str) -> None:
    """门禁脚本（本地数据，但逐 bar 全量回放，分钟级 → 慢档）。"""
    _run_script(name, timeout=1800)
