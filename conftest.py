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
