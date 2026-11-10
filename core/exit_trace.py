# -*- coding: utf-8 -*-
"""H80：进程退出留痕 —— start / exit 成对写入，缺 exit 即判 `untraced`。

为什么需要（2026-11-10 立号）
=============================
  11-10 本机 08:36:31 / 09:07:27 两次实例消失：`live_*.log` 里
  `Traceback` / `主循环致命退出` / `[exhausted]` / `观察期结束` **四类文案零命中**，
  没有退出码、没有结束时刻、没有死因 ⇒ 只能事后靠"进程不见了"发现（判据 A）。
  既有 11 次 `Fatal Python error` 转储中 10 次的当前线程都是
  `tgw/interface.py:22 Login` → `data_fetch._worker`，即 **AD SDK 在非主线程里
  直接终止整个进程**；这类退出在 Python 侧没有任何异常可抓。

三种退出模式的判别（本模块的判据）
==================================
  | 模式                        | faulthandler 转储 | atexit | 本文件 |
  |-----------------------------|-------------------|--------|--------|
  | Python 正常返回 / SystemExit| 无                | 执行    | 有 exit |
  | SIGABRT / SIGSEGV（原生崩溃）| **有**            | 不执行 | **无 exit** ⇒ untraced |
  | `exit(0)` / TerminateProcess | 无                | 视实现  | **无 exit** ⇒ untraced |

  ⇒ `untraced` 不是"没记"，而是**结论**：进程不是从 Python 层退出的。

设计约束
========
1. **零依赖、零副作用**：只有 live 入口 `install()`，回测/补数永不安装。
2. **mark 立即落盘**：信号路径下 atexit 可能不执行，所以 mark 当场写，不攒着。
3. **幂等**：一个进程最多一条 start、一条 exit；重复 mark 不覆盖首条。
4. **不吞异常**：excepthook 记录后仍调用原 hook，不改变退出码。
5. **线程异常只记不退出**：线程死亡 ≠ 进程死亡，单独记 `thread_exception`
   （这条同时给 H60/H79 的"进程在但零成功"提供证据）。
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

KIND_NORMAL = "normal_return"
KIND_UNTRACED = "untraced"


class ExitTrace:
    """一个进程一份的退出留痕器（可注入 path / clock，便于测试）。"""

    def __init__(self, path, started_at: datetime | None = None,
                 snapshot=None, argv=None, pid: int | None = None):
        self.path = Path(path)
        self.started_at = started_at or datetime.now()
        self._t0 = time.monotonic()
        self._snapshot = snapshot
        self._argv = list(sys.argv if argv is None else argv)
        self.pid = int(os.getpid() if pid is None else pid)
        self._kind: str | None = None
        self._detail: dict = {}
        self._stage = "init"
        self._lock = threading.Lock()
        self._exit_written = False
        self._installed = False

    # ---------- 写入 ----------
    def _append(self, rec: dict) -> None:
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())      # 崩溃前必须已落盘
                    except OSError:
                        pass
            except Exception:                     # noqa: BLE001 —— 留痕失败不得影响进程
                pass

    def _base(self, event: str) -> dict:
        rec = {
            "event": event,
            "pid": self.pid,
            "at": datetime.now().isoformat(timespec="seconds"),
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "uptime_s": round(time.monotonic() - self._t0, 1),
            "stage": self._stage,
            "argv": self._argv,
        }
        if self._snapshot is not None:
            try:
                snap = self._snapshot() or {}
                if isinstance(snap, dict):
                    rec["heartbeat"] = snap
            except Exception:                      # noqa: BLE001
                pass
        return rec

    # ---------- 对外 ----------
    def set_stage(self, stage: str) -> None:
        """标记当前所处阶段（startup / collect / match / sleep / close）。"""
        self._stage = str(stage)

    def mark(self, kind: str, **detail) -> None:
        """记录退出原因并立即落盘（幂等：只记第一次）。"""
        with self._lock:
            if self._exit_written:
                return
            self._exit_written = True
            self._kind = str(kind)
            self._detail = dict(detail)
        rec = self._base("exit")
        rec["exit_kind"] = self._kind
        rec["detail"] = self._detail
        self._append(rec)

    def mark_thread_exception(self, args) -> None:
        """Thread 异常：只记事件，不改进程状态。"""
        rec = self._base("thread_exception")
        rec["thread"] = getattr(getattr(args, "thread", None), "name", None)
        exc_type = getattr(args, "exc_type", None)
        exc_value = getattr(args, "exc_value", None)
        rec["exception"] = getattr(exc_type, "__name__", str(exc_type))
        rec["message"] = str(exc_value)[:300]
        self._append(rec)

    def finish(self) -> None:
        """解释器正常收尾时的兜底：未 mark 过就按 normal_return 记（幂等）。

        注意：`sys.exit(0)` 抛出的 `SystemExit` **不会**走到 sys.excepthook，
        所以入口必须自己捕获并 `mark("SystemExit(...)")`；否则只能记成
        `normal_return` —— 这也是 live_run 顶层改成 `except SystemExit` 的原因。
        """
        self.mark(KIND_NORMAL, via="atexit")

    # ---------- 安装 ----------
    def install(self, register_atexit: bool = True,
                install_signals: bool = True) -> "ExitTrace":
        if self._installed:
            return self
        self._installed = True
        self._append(self._base("start"))

        prev_hook = sys.excepthook

        def _excepthook(exc_type, exc_value, exc_tb):
            self.mark(f"Exception({getattr(exc_type, '__name__', exc_type)})",
                      message=str(exc_value)[:300],
                      trace="".join(traceback.format_exception(exc_type, exc_value, exc_tb))[-1500:])
            try:
                prev_hook(exc_type, exc_value, exc_tb)
            except Exception:                     # noqa: BLE001
                pass

        sys.excepthook = _excepthook

        prev_thread_hook = getattr(threading, "excepthook", None)

        def _thread_hook(args):
            try:
                self.mark_thread_exception(args)
            finally:
                if prev_thread_hook is not None:
                    prev_thread_hook(args)

        threading.excepthook = _thread_hook

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            if not install_signals:
                break
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._signal_handler(sig))
            except (ValueError, OSError):
                pass

        def _on_exit():
            # 走到这里说明 Python 解释器正常收尾；若已 mark 则不会覆盖。
            self.finish()

        if register_atexit:
            atexit.register(_on_exit)
        return self

    def _signal_handler(self, sig):
        def _handler(signum, frame):              # noqa: ARG001
            self.mark(f"signal({signum})", via="signal_handler")
            try:
                signal.signal(signum, signal.SIG_DFL)
                os.kill(os.getpid(), signum)
            except Exception:                     # noqa: BLE001
                os._exit(128 + int(signum))       # 兜底：不能留在 handler 里
        return _handler


def install(path, started_at: datetime | None = None, snapshot=None,
            **kwargs) -> ExitTrace:
    """live 入口专用：安装并写入 start 记录（kwargs 透传 install 的开关）。"""
    return ExitTrace(path, started_at=started_at, snapshot=snapshot).install(**kwargs)
