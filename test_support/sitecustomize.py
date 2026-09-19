# -*- coding: utf-8 -*-
"""sitecustomize.py —— 让**子进程**也带上离线守卫（H19 / A7 第 2 道锁）。

为什么需要
----------
`tests/test_scripts_smoke.py` 用 `subprocess.run([sys.executable, ...])` 拉起
门禁脚本（`test_sentiment_t_dual_gate.py` 等）。**子进程有全新的解释器状态，
父进程的 monkeypatch 穿不过去**。而其中两个脚本会经
`SentimentTStrategy.run()` → `sentiment_data` → `data_fetch._login()`
真实登录 AmazingData —— 与观察期采集互斥。

工作原理
--------
Python 启动时会自动 import `sitecustomize`（只要它在 `sys.path` 上）。
所以只要让子进程带上本目录：

    PYTHONPATH=<project>\\test_support

子进程就会自动装上守卫，**无需改动被测脚本一行代码**。

⚠️ 本模块只做一件事：**在测试会话里**才安装守卫。为避免误伤正常业务运行，
它用环境变量 `QUANT_OFFLINE_GUARD=1` 作为开关 —— `conftest.py` 会设置它，
`tests/test_scripts_smoke.py` 显式传给子进程。手工跑 `python test_fetch.py`
不设该变量 ⇒ 完全不受影响。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if os.environ.get("QUANT_OFFLINE_GUARD") == "1":
    _app = Path(__file__).resolve().parents[1]
    if str(_app) not in sys.path:
        sys.path.insert(0, str(_app))
    try:
        import tests_offline_guard as _guard

        _guard.install("subprocess/sitecustomize")
    except Exception as _exc:  # noqa: BLE001  守卫失败不得拖垮子进程
        sys.stderr.write(f"[offline-guard] 子进程守卫安装失败：{_exc!r}\n")
