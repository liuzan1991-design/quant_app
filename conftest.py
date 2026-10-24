# -*- coding: utf-8 -*-
"""pytest 全局配置 —— 测试路径禁止真实登录（H19 / A7）。

放在**项目根**（不是 `tests/`）是必需的：`test_app.py` / `test_ui_quick.py` /
`test_batch_ui.py` / `test_server.py` 都在根目录，pytest 只加载**收集目录及其
上级**的 conftest.py。放在 `tests/` 里盖不住它们。

守卫本身见 `tests_offline_guard.py`（三道锁的说明也在那里）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

# 锁 2 的开关：让 subprocess 子进程（sitecustomize.py）也装上守卫
os.environ["QUANT_OFFLINE_GUARD"] = "1"

import tests_offline_guard as guard  # noqa: E402

# 锁 1：同进程 monkeypatch（AppTest 与直接调用都在本进程内执行，可穿透）
guard.install("conftest")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ad_session_state():
    """H34：`data_fetch` 的会话治理状态（熔断计数/重建配额/日历缓存）是模块级单例。

    不每个用例复位，前一个文件的失败游程会累到后一个文件 ⇒ 后者的"正常路径"
    被熔断快速失败打死，红得像功能坏了（与 `_AD_CACHE` 跨文件污染同一类坑）。
    """
    from core import data_fetch
    data_fetch.reset_session_state()
    yield
    data_fetch.reset_session_state()



@pytest.fixture(autouse=True)
def _isolate_alerts(tmp_path, monkeypatch):
    """H55：告警写盘一律隔离到 tmp。

    起因：`test_watchdog_staleness.py` 跑 `watchdog.main()` 时没隔离告警模块，
    真实 `raise_alert` 把 `live_outputs/ALERT_ACTIVE.json` 与**桌面横幅**都写了，
    在周六（无采集）造出一条"采集已停摆 3001s"的假告警。
    与 H19（测试触发真实登录）同族：**副作用的落点没被纳入测试边界**。
    """
    from core import alerts
    root = tmp_path / "_alerts"
    monkeypatch.setattr(alerts, "LIVE_DIR", root)
    monkeypatch.setattr(alerts, "LOG_DIR", root / "logs")
    monkeypatch.setattr(alerts, "ACTIVE_FILE", root / "ALERT_ACTIVE.json")
    monkeypatch.setattr(alerts, "JSONL_FILE", root / "logs" / "alerts.jsonl")
    monkeypatch.setattr(alerts, "DESKTOP_FILE", root / "Desktop" / "量化系统告警.txt")
    yield
