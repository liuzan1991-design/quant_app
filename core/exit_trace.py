# -*- coding: utf-8 -*-
"""H80退出观测：run_id配对、阶段持久化。缺exit不等于已退出，更不等于死因。
写入失败保留首次原因供后续补记；仅live入口显式install才安装hook。
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
import uuid
from datetime import datetime
from pathlib import Path

KIND_NORMAL = "normal_return"
KIND_UNTRACED = "untraced"  # 兼容旧名称；仅已确认结束且缺exit，死因仍未知


def _process_create_time(pid: int) -> float | None:
    """可选的只读进程出生标识；不可获取时明确未知。"""
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None


class ExitTrace:
    def __init__(self, path, started_at: datetime | None = None,
                 snapshot=None, argv=None, pid: int | None = None,
                 run_id: str | None = None):
        self.path = Path(path)
        self.started_at = started_at or datetime.now()
        self._t0 = time.monotonic()
        self._snapshot = snapshot
        self._argv = list(sys.argv if argv is None else argv)
        self.pid = int(os.getpid() if pid is None else pid)
        self.run_id = run_id or uuid.uuid4().hex
        self.process_create_time = (_process_create_time(self.pid)
                                    if self.pid == os.getpid() else None)
        self._stage = "init"
        self._lock = threading.RLock()  # 信号打断本线程时允许重入
        self._start_written = False
        self._exit_written = False
        self._installed = False
        self._pending_exit: dict | None = None
        self._start_record: dict | None = None
        self.write_failures = 0
        self.last_write_error: str | None = None

    def _needs_leading_newline(self) -> bool:
        """仅当文件末字节不是换行时才前置补一个 —— 隔开上次被截断的残行。

        H80 首日实测：原实现无条件前置 "\\n"，而每条记录本身已以 "\\n" 结尾
        ⇒ 文件里每隔一行就是空行，不成严格 JSON Lines（jq / pandas lines=True
        都会报错）。补换行只在"尾行确实残缺"时才需要。
        """
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return False
            with open(self.path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) != b"\n"
        except Exception:
            # 判不了尾字节就不前置 —— 宁可少一个分隔符，也不能因此阻断留痕写入。
            return False

    def _append(self, rec: dict) -> bool:
        """完整write/flush/fsync才算成功。仅尾行残缺时前置换行（保持严格 JSONL）。
        fsync失败可能已留下完整行；补写复用event_id，分析器负责去重。
        """
        try:
            lead = "\n" if self._needs_leading_newline() else ""
            data = (lead + json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as fh:
                if fh.write(data) != len(data):
                    raise OSError("short trace write")
                fh.flush()
                os.fsync(fh.fileno())
            return True
        except Exception as exc:
            self.write_failures += 1
            self.last_write_error = type(exc).__name__
            try:
                sys.stderr.write(f"[H80] trace write failed ({self.last_write_error}); retry remains enabled\n")
            except Exception:
                pass
            return False

    def _base(self, event: str, *, snapshot: bool = True) -> dict:
        rec = {
            "schema_version": 2,
            "event": event,
            "event_id": uuid.uuid4().hex,
            "run_id": self.run_id,
            "pid": self.pid,
            "process_create_time": self.process_create_time,
            "at": datetime.now().isoformat(timespec="microseconds"),
            "started_at": self.started_at.isoformat(timespec="microseconds"),
            "uptime_s": round(time.monotonic() - self._t0, 3),
            "stage": self._stage,
            "argv": self._argv,
            "write_failures": self.write_failures,
        }
        if snapshot and self._snapshot is not None:
            try:
                snap = self._snapshot() or {}
                if isinstance(snap, dict):
                    rec["heartbeat"] = snap
            except Exception:
                pass
        return rec

    def _ensure_start(self) -> bool:
        if not self._start_written:
            if self._start_record is None:
                self._start_record = self._base("start")
            self._start_written = self._append(self._start_record)
        return self._start_written

    def set_stage(self, stage: str) -> bool:
        """每次阶段进入均持久化；留痕失败不改变交易行为。"""
        with self._lock:
            self._stage = str(stage)
            if not self._ensure_start():
                return False
            return self._append(self._base("stage"))

    def _mark(self, kind: str, detail: dict, *, blocking: bool = True) -> bool:
        if not self._lock.acquire(blocking=blocking):
            return False
        try:
            if self._exit_written:
                return True
            if self._pending_exit is None:
                self._pending_exit = self._base("exit", snapshot=blocking)
                self._pending_exit.update(exit_kind=str(kind), detail=dict(detail))
            if not self._ensure_start():
                return False
            self._exit_written = self._append(self._pending_exit)
            return self._exit_written
        finally:
            self._lock.release()

    def mark(self, kind: str, **detail) -> bool:
        """首次原因不覆盖；写失败允许补记，写成功后幂等。"""
        return self._mark(kind, detail)

    def mark_thread_exception(self, args) -> None:
        with self._lock:
            if not self._ensure_start():
                return
            rec = self._base("thread_exception")
            rec["thread"] = getattr(getattr(args, "thread", None), "name", None)
            rec["exception"] = getattr(getattr(args, "exc_type", None), "__name__", None)
            rec["message"] = str(getattr(args, "exc_value", ""))[:300]
            self._append(rec)

    def finish(self) -> None:
        # 未捕获SystemExit也会执行atexit；须由入口另行捕获，不能由atexit推死因。
        self.mark(KIND_NORMAL, via="atexit")

    def install(self, register_atexit: bool = True,
                install_signals: bool = True) -> "ExitTrace":
        if self._installed:
            return self
        self._installed = True
        with self._lock:
            self._ensure_start()
        prev_hook = sys.excepthook

        def _excepthook(exc_type, exc_value, exc_tb):
            try:
                self.mark(f"Exception({getattr(exc_type, '__name__', exc_type)})",
                          message=str(exc_value)[:300],
                          trace="".join(traceback.format_exception(exc_type, exc_value, exc_tb))[-1500:])
            finally:
                prev_hook(exc_type, exc_value, exc_tb)

        sys.excepthook = _excepthook
        prev_thread_hook = getattr(threading, "excepthook", None)

        def _thread_hook(args):
            try:
                self.mark_thread_exception(args)
            finally:
                if prev_thread_hook is not None:
                    prev_thread_hook(args)

        threading.excepthook = _thread_hook
        if install_signals:
            for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is None:
                    continue
                try:
                    previous = signal.getsignal(sig)
                    if previous != signal.SIG_IGN:
                        signal.signal(sig, self._signal_handler(sig, previous))
                except (ValueError, OSError):
                    pass
        if register_atexit:
            atexit.register(self.finish)
        return self

    def _signal_handler(self, sig, previous=None):
        if previous is None:
            previous = signal.getsignal(sig)

        def _handler(signum, frame):
            # 不能等待其他线程持有的锁，观测失败不能阻止原信号处理。
            if self._lock.acquire(blocking=False):
                try:
                    if self._ensure_start():
                        rec = self._base("signal", snapshot=False)
                        rec["signal"] = signum
                        self._append(rec)
                finally:
                    self._lock.release()
            if callable(previous):
                # 保留自定义处理器与SIGINT默认KeyboardInterrupt语义。
                return previous(signum, frame)
            if previous == signal.SIG_IGN:
                return None
            self._mark(f"signal({signum})", {"via": "signal_handler"}, blocking=False)
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        return _handler


def install(path, started_at: datetime | None = None, snapshot=None, **kwargs) -> ExitTrace:
    return ExitTrace(path, started_at=started_at, snapshot=snapshot).install(**kwargs)
