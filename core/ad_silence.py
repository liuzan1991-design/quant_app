# -*- coding: utf-8 -*-
"""星耀数智 SDK 登录输出抑制（共享工具）。

星耀 SDK 的 ad.login() 会把会话 Token 和权限列表直接打印到命令输出，
且由底层 C 库(tgw)写 fd 1/2，Python 层 redirect_stdout 拦不住。
所有直接调用 ad.login() 的脚本/测试都必须用 suppress_sdk_output() 包住登录调用，
防止 Token 泄漏到控制台、日志和测试结果。
"""
from __future__ import annotations

import contextlib
import io
import os


@contextlib.contextmanager
def suppress_sdk_output():
    """抑制星耀 SDK login 的 stdout/stderr 输出（Python 层 + C 层 fd 1/2）。

    登录为一次性调用，短暂把进程 fd 1/2 重定向到空设备可接受；
    Python 层输出捕获到缓冲区（供需要时检测登录状态）。
    """
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        saved_out, saved_err = os.dup(1), os.dup(2)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_fd, 1)
            os.dup2(null_fd, 2)
            yield sink
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            os.close(saved_out)
            os.close(saved_err)
            os.close(null_fd)
