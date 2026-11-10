# -*- coding: utf-8 -*-
"""H80：进程退出留痕的验收测试。

分两层：
1. **单元层**：写入格式、幂等、快照失败不影响、hook 委派、阶段传播。
2. **真实子进程层**（不用替身）：真的 `sys.exit` / `os._exit` / 未捕获异常，
   验证「有 exit」与「untraced」两种结论都成立 —— 这是 H80 判据的核心：
   **untraced 不是"没记"，而是"不是从 Python 层退出的"**。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "scripts"))

from core import exit_trace  # noqa: E402
import analyze_exit_trace as an  # noqa: E402


def _recs(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _install(tmp_path, **kw):
    return exit_trace.install(tmp_path / "exit_trace.jsonl",
                              register_atexit=False, install_signals=False, **kw)


# ---------------------------------------------------------------- 单元层
def test_start_record_has_identity(tmp_path):
    t = _install(tmp_path, started_at=__import__("datetime").datetime(2026, 11, 10, 9, 10, 4))
    recs = _recs(t.path)
    assert len(recs) == 1
    r = recs[0]
    assert r["event"] == "start"
    assert r["pid"] == os.getpid()
    assert r["started_at"] == "2026-11-10T09:10:04"
    assert isinstance(r["argv"], list)


def test_finish_records_normal_return(tmp_path):
    t = _install(tmp_path)
    t.finish()
    ex = [r for r in _recs(t.path) if r["event"] == "exit"]
    assert len(ex) == 1
    assert ex[0]["exit_kind"] == "normal_return"
    assert ex[0]["detail"]["via"] == "atexit"


def test_mark_is_idempotent(tmp_path):
    t = _install(tmp_path)
    t.mark("SystemExit(0)", via="top_level")
    t.mark("signal(15)")
    t.finish()
    ex = [r for r in _recs(t.path) if r["event"] == "exit"]
    assert len(ex) == 1, "一个进程只能有一条 exit 记录"
    assert ex[0]["exit_kind"] == "SystemExit(0)"


def test_stage_is_carried_into_exit(tmp_path):
    t = _install(tmp_path)
    t.set_stage("sleep")
    t.mark("Exception(RuntimeError)")
    assert [r for r in _recs(t.path) if r["event"] == "exit"][0]["stage"] == "sleep"


def test_snapshot_failure_does_not_block_record(tmp_path):
    def boom():
        raise RuntimeError("heartbeat 读不到")

    t = exit_trace.install(tmp_path / "e.jsonl", snapshot=boom,
                           register_atexit=False, install_signals=False)
    t.mark("SystemExit(0)")
    ex = [r for r in _recs(t.path) if r["event"] == "exit"]
    assert len(ex) == 1 and "heartbeat" not in ex[0]


def test_snapshot_is_attached(tmp_path):
    t = _install(tmp_path, snapshot=lambda: {"status": "running", "collect_errors": 0})
    t.mark("SystemExit(0)")
    ex = [r for r in _recs(t.path) if r["event"] == "exit"][0]
    assert ex["heartbeat"] == {"status": "running", "collect_errors": 0}


def test_thread_exception_recorded_without_exit(tmp_path):
    class _Args:
        exc_type = RuntimeError
        exc_value = RuntimeError("ad login fail")
        thread = type("T", (), {"name": "ad-login"})()

    t = _install(tmp_path)
    t.mark_thread_exception(_Args())
    recs = _recs(t.path)
    assert [r["event"] for r in recs] == ["start", "thread_exception"]
    assert recs[1]["thread"] == "ad-login"
    assert recs[1]["exception"] == "RuntimeError"


def test_excepthook_records_and_delegates(tmp_path):
    """装 hook 时捕获的旧 hook 必须仍被调用（不得吞掉异常输出）。"""
    seen = []
    prev = sys.excepthook
    sys.excepthook = lambda *a: seen.append(a[0].__name__)
    try:
        t = exit_trace.install(tmp_path / "h2.jsonl",
                               register_atexit=False, install_signals=False)
        try:
            raise ValueError("boom")
        except ValueError:
            sys.excepthook(*sys.exc_info())
    finally:
        sys.excepthook = prev
    assert seen == ["ValueError"], "原 hook 必须被调用（不得吞异常输出）"
    ex = [r for r in _recs(t.path) if r["event"] == "exit"]
    assert ex and ex[0]["exit_kind"] == "Exception(ValueError)"


# ---------------------------------------------------------------- 验收视图
def test_summarize_marks_untraced():
    recs = [
        {"event": "start", "pid": 1, "started_at": "2026-11-10T09:10:04", "stage": "sleep"},
        {"event": "start", "pid": 2, "started_at": "2026-11-10T09:40:00", "stage": "match"},
        {"event": "exit", "pid": 2, "exit_kind": "SystemExit(0)", "uptime_s": 60.0,
         "stage": "sleep"},
    ]
    s = an.summarize(recs)
    assert s["total"] == 2
    assert [i["pid"] for i in s["untraced"]] == ["1"]
    assert s["kinds"]["untraced"] == 1 and s["kinds"]["SystemExit(0)"] == 1
    assert "untraced" in an.render(s)


def test_summarize_empty_is_not_reported_as_healthy():
    """0 实例必须报"还没观测"，不能打 ✅（假绿）。"""
    s = an.summarize([])
    text = an.render(s)
    assert "✅" not in text
    assert "还没观测" in text


def test_summarize_detects_short_lived():
    recs = [
        {"event": "start", "pid": 3, "started_at": "2026-11-10T08:25:07", "stage": "collect"},
        {"event": "exit", "pid": 3, "exit_kind": "untraced", "uptime_s": 120.0,
         "stage": "sleep"},
    ]
    s = an.summarize(recs)
    assert len(s["short_lived"]) == 1
    assert "短命实例" in an.render(s)


# ---------------------------------------------------------------- 真实子进程
def _run(tmp_path, body: str) -> subprocess.CompletedProcess:
    """把 header 与 body 直接拼接（不能整体 dedent：body 已无缩进会打断公共前缀）。"""
    trace = tmp_path / "trace.jsonl"
    header = (
        "import sys, os\n"
        f'sys.path.insert(0, r"{PROJECT}")\n'
        "from core.exit_trace import install\n"
        f'TRACE = install(r"{trace}")\n'
    )
    script = tmp_path / "child.py"
    script.write_text(header + textwrap.dedent(body), encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(PROJECT))
    return subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, timeout=90, env=env, cwd=str(PROJECT))


def test_subprocess_systemexit_is_traced(tmp_path):
    """模拟 live_run 顶层：捕获 SystemExit 先 mark，再退出。"""
    r = _run(tmp_path, textwrap.dedent("""
        try:
            sys.exit(0)
        except SystemExit as exc:
            TRACE.mark("SystemExit(%r)" % (exc.code,), via="top_level")
            raise
    """))
    recs = _recs(tmp_path / "trace.jsonl")
    ex = [x for x in recs if x["event"] == "exit"]
    assert ex and ex[0]["exit_kind"] == "SystemExit(0)"
    assert r.returncode == 0


def test_subprocess_os_exit_leaves_untraced(tmp_path):
    """os._exit 不执行 atexit ⇒ 只有 start，没有 exit ⇒ untraced（真实验证）。"""
    _run(tmp_path, "os._exit(0)")
    recs = _recs(tmp_path / "trace.jsonl")
    assert [x["event"] for x in recs] == ["start"], \
        "os._exit 走不到 atexit，必须只剩 start（否则 untraced 判据不成立）"
    assert an.summarize(recs)["untraced"]


def test_subprocess_uncaught_exception_is_traced(tmp_path):
    r = _run(tmp_path, "raise RuntimeError('sdk boom')")
    recs = _recs(tmp_path / "trace.jsonl")
    ex = [x for x in recs if x["event"] == "exit"]
    assert ex and ex[0]["exit_kind"] == "Exception(RuntimeError)"
    assert ex[0]["detail"]["message"] == "sdk boom"
    assert r.returncode != 0
