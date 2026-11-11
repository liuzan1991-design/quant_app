# -*- coding: utf-8 -*-
"""H80：进程退出留痕的验收测试。

分两层：
1. **单元层**：写入格式、幂等、快照失败不影响、hook 委派、阶段传播。
2. **真实子进程层**（不用替身）：真的 `sys.exit` / `os._exit` / 未捕获异常，
   验证「有 exit」与「untraced」两种结论都成立 —— 这是 H80 判据的核心：
   **缺exit仅表示缺记录；须另核存活，不能据此倒推死因**。
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


@pytest.fixture(autouse=True)
def _restore_hooks():
    import threading
    old_sys, old_thread = sys.excepthook, threading.excepthook
    yield
    sys.excepthook, threading.excepthook = old_sys, old_thread


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
    assert r["started_at"] == "2026-11-10T09:10:04.000000"
    assert r["run_id"] and r["event_id"]
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
         "stage": "sleep", "started_at": "2026-11-10T09:40:00"},
    ]
    s = an.summarize(recs, {"legacy:1:2026-11-10T09:10:04": "stopped"})
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
        {"event": "exit", "pid": 3, "exit_kind": "normal_return", "uptime_s": 120.0,
         "stage": "sleep", "started_at": "2026-11-10T08:25:07"},
    ]
    s = an.summarize(recs)
    assert len(s["short_lived"]) == 1
    assert "短存活退出记录" in an.render(s)


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
    env = dict(os.environ, PYTHONPATH=str(PROJECT), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-X", "utf8", "-B", str(script)], capture_output=True,
                          text=True, encoding="utf-8", timeout=15, env=env, cwd=str(PROJECT))


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
    assert an.summarize(recs, {recs[0]["run_id"]: "stopped"})["untraced"]


def test_subprocess_uncaught_exception_is_traced(tmp_path):
    r = _run(tmp_path, "raise RuntimeError('sdk boom')")
    recs = _recs(tmp_path / "trace.jsonl")
    ex = [x for x in recs if x["event"] == "exit"]
    assert ex and ex[0]["exit_kind"] == "Exception(RuntimeError)"
    assert ex[0]["detail"]["message"] == "sdk boom"
    assert r.returncode != 0


# ------------------------------------------------ H80第二轮边界与故障注入
def _event(event, run="r1", **over):
    row = dict(event=event, run_id=run, pid=123, started_at="2026-11-10T04:30:00",
               process_create_time=100.0, stage="init")
    row.update(over)
    return row


@pytest.mark.parametrize("alive,status", [
    ("running", "running"), ("unknown", "unknown"), ("stopped", "missing_exit")])
def test_no_exit_requires_independent_liveness(alive, status):
    s = an.summarize([_event("start")], {"r1": alive})
    assert s["instances"][0]["status"] == status
    assert len(s["missing_exit"]) == (alive == "stopped")
    assert "非 Python 层退出" not in an.render(s)
    assert "✅" not in an.render(s)


def test_run_id_prevents_pid_reuse_false_green():
    rows = [_event("start", "old"),
            _event("exit", "old", exit_kind="normal_return"),
            _event("start", "new", started_at="2026-11-11T04:30:00")]
    s = an.summarize(rows, {"old": "stopped", "new": "running"})
    assert s["total"] == 2 and len(s["running"]) == 1
    assert s["running"][0]["run_id"] == "new"
    assert s["running"][0]["exit_kind"] is None


def test_legacy_pid_only_exit_is_not_guessed():
    s = an.summarize([_event("start"),
                      {"event": "exit", "pid": 123, "exit_kind": "normal_return"}])
    assert s["unknown"] and s["log_issues"]
    assert s["instances"][0]["exit_kind"] is None


def test_stage_survives_real_abrupt_exit(tmp_path):
    r = _run(tmp_path, 'TRACE.set_stage("collect")\nTRACE.set_stage("sleep")\nos._exit(0)')
    assert r.returncode == 0
    rows = an.load(tmp_path / "trace.jsonl")
    s = an.summarize(rows, {rows[0]["run_id"]: "stopped"})
    assert s["missing_exit"][0]["last_stage"] == "sleep"
    assert s["missing_exit"][0]["cause"] == "undetermined"


def test_stage_is_persisted_on_every_entry(tmp_path):
    t = _install(tmp_path)
    assert t.set_stage("collect") and t.set_stage("collect")
    assert [r["stage"] for r in _recs(t.path) if r["event"] == "stage"] == ["collect", "collect"]


@pytest.mark.parametrize("failed_op", ["open", "fsync"])
def test_exit_write_failure_can_retry_original_reason(tmp_path, monkeypatch, failed_op):
    import builtins
    t = _install(tmp_path)
    target, attr = (builtins, "open") if failed_op == "open" else (exit_trace.os, "fsync")
    original = getattr(target, attr)

    def fail(*a, **kw):
        raise OSError("injected trace IO failure")
    monkeypatch.setattr(target, attr, fail)
    assert t.mark("SystemExit(7)", via="first") is False
    assert not t._exit_written and t.write_failures == 1
    monkeypatch.setattr(target, attr, original)
    t.finish()
    s = an.summarize(an.load(t.path))
    assert not s["log_issues"]  # fsync失败留下完整行时按event_id去重
    assert s["instances"][0]["exit_kind"] == "SystemExit(7)"
    assert s["instances"][0]["detail"] == {"via": "first"}
    assert t._exit_written


def test_failed_start_can_be_retried_before_stage(tmp_path, monkeypatch):
    original = exit_trace.os.fsync
    monkeypatch.setattr(exit_trace.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    t = _install(tmp_path)
    assert not t._start_written
    monkeypatch.setattr(exit_trace.os, "fsync", original)
    assert t.set_stage("collect")
    s = an.summarize(an.load(t.path))
    assert s["total"] == 1 and not s["log_issues"]
    assert s["instances"][0]["last_stage"] == "collect"


def test_partial_write_then_retry_does_not_swallow_next_record(tmp_path, monkeypatch):
    import builtins
    t = _install(tmp_path)
    original = builtins.open

    class Partial:
        def __enter__(self):
            self.fh = original(t.path, "ab")
            return self
        def write(self, data):
            self.fh.write(data[:25])
            self.fh.flush()
            raise OSError("partial write")
        def __exit__(self, *args):
            self.fh.close()

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", lambda *a, **k: Partial())
        assert not t.mark("SystemExit(9)")
    t.finish()
    s = an.summarize(an.load(t.path))
    assert s["instances"][0]["exit_kind"] == "SystemExit(9)"
    assert any(i["code"] == "invalid_json_line" for i in s["log_issues"])
    assert "配对完整" not in an.render(s)


def test_jsonl_has_no_blank_lines(tmp_path):
    """严格 JSON Lines：每条记录独占一行，中间不得夹空行。

    H80 首日实测缺陷：原实现无条件前置 "\\n"，而记录本身已以 "\\n" 结尾
    ⇒ 文件成「记录/空行」交替（1478 条记录 2957 行），jq、pandas lines=True
    之类标准读取器直接报错。这条用例把它锁死。
    """
    t = _install(tmp_path)
    t.set_stage("collect")
    t.set_stage("match")
    t.finish()
    raw = t.path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert all(x.strip() for x in lines), "不得出现空行"
    for x in lines:
        json.loads(x)          # 每行必须独立可解析
    assert len(lines) == len(_recs(t.path))


def test_leading_newline_only_when_tail_truncated(tmp_path):
    """残行隔离照旧有效：只有尾行确实残缺时才补前置换行。"""
    t = _install(tmp_path)
    with open(t.path, "ab") as fh:          # 模拟被强杀留下的半截记录
        fh.write(b'{"event":"stage","stage":"col')
    t.mark("SystemExit(9)")
    lines = t.path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["event"] == "start"
    assert lines[1] == '{"event":"stage","stage":"col'   # 残行独立成行，未被吞掉
    assert json.loads(lines[2])["exit_kind"] == "SystemExit(9)"
    assert all(x.strip() for x in lines)
    assert len(lines) == 3                  # start + 残行 + exit


@pytest.mark.parametrize("bad", [b'{"event":"exit"', b'\xff\xfe\n', b'[]\n', b'null\n'])
def test_corrupt_or_truncated_log_is_visible(tmp_path, bad):
    p = tmp_path / "bad.jsonl"
    p.write_bytes((json.dumps(_event("start")) + "\n").encode() + bad)
    s = an.summarize(an.load(p))
    assert s["log_integrity"] == "invalid" and s["log_issues"]
    assert "✅" not in an.render(s) and "配对完整" not in an.render(s)


def test_truncated_exit_never_becomes_recorded_exit(tmp_path):
    p = tmp_path / "cut.jsonl"
    p.write_text(json.dumps(_event("start")) + '\n{"event":"exit"', encoding="utf-8")
    s = an.summarize(an.load(p), {"r1": "stopped"})
    assert s["missing_exit"] and s["log_issues"]


def test_missing_log_is_not_healthy(tmp_path):
    s = an.summarize(an.load(tmp_path / "absent.jsonl"))
    assert s["total"] == 0 and s["log_issues"][0]["code"] == "log_missing"
    assert "还没观测" in an.render(s)


def test_orphan_or_conflicting_events_are_reported():
    s = an.summarize([_event("exit", exit_kind="normal_return")])
    assert s["log_issues"][0]["code"] == "orphan_events"
    rows = [_event("start"), _event("exit", pid=999, exit_kind="normal_return")]
    s = an.summarize(rows)
    assert any(x["code"] == "conflicting_instance_identity" for x in s["log_issues"])
    assert "配对完整" not in an.render(s)


def test_live_probe_checks_exact_birth_and_access_failures():
    import psutil

    class Proc:
        def create_time(self):
            return 101.0
    inst = an.summarize([_event("start")])["instances"]
    assert an.probe_liveness(inst, lambda pid: Proc()) == {"r1": "stopped"}

    def denied(pid):
        raise psutil.AccessDenied(pid)
    assert an.probe_liveness(inst, denied) == {"r1": "unknown"}

    def gone(pid):
        raise psutil.NoSuchProcess(pid)
    assert an.probe_liveness(inst, gone) == {"r1": "stopped"}


def test_live_probe_without_birth_cannot_claim_running():
    class Proc:
        def create_time(self):
            return 100.0
    inst = an.summarize([_event("start", process_create_time=None)])["instances"]
    assert an.probe_liveness(inst, lambda pid: Proc()) == {"r1": "unknown"}


def test_signal_custom_handler_can_continue_without_false_exit(tmp_path):
    import signal
    seen = []
    t = _install(tmp_path)
    handler = t._signal_handler(signal.SIGINT, lambda signum, frame: seen.append(signum))
    handler(signal.SIGINT, None)
    assert seen == [signal.SIGINT]
    assert [r["event"] for r in _recs(t.path)] == ["start", "signal"]
    assert not t._exit_written


def test_signal_default_interrupt_is_preserved(tmp_path):
    import signal
    t = _install(tmp_path)
    handler = t._signal_handler(signal.SIGINT, signal.default_int_handler)
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)
    assert not t._exit_written


def test_ignored_signals_are_not_overridden(tmp_path, monkeypatch):
    import signal
    calls = []
    monkeypatch.setattr(signal, "getsignal", lambda *a: signal.SIG_IGN)
    monkeypatch.setattr(signal, "signal", lambda *a: calls.append(a))
    exit_trace.install(tmp_path / "ignored.jsonl", register_atexit=False)
    assert calls == []


def test_default_termination_records_then_delegates(tmp_path, monkeypatch):
    import signal
    t = _install(tmp_path)
    calls = []
    monkeypatch.setattr(signal, "signal", lambda *a: calls.append(("restore", a)))
    monkeypatch.setattr(os, "kill", lambda *a: calls.append(("kill", a)))
    t._signal_handler(signal.SIGTERM, signal.SIG_DFL)(signal.SIGTERM, None)
    assert [c[0] for c in calls] == ["restore", "kill"]
    assert _recs(t.path)[-1]["exit_kind"] == f"signal({signal.SIGTERM})"


def test_signal_io_failure_does_not_swallow_previous_handler(tmp_path, monkeypatch):
    import signal
    t = _install(tmp_path)
    monkeypatch.setattr(exit_trace.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(KeyboardInterrupt):
        t._signal_handler(signal.SIGINT, signal.default_int_handler)(signal.SIGINT, None)


def test_signal_same_thread_reentry_does_not_deadlock(tmp_path):
    r = _run(tmp_path, """
        import signal
        with TRACE._lock:
            TRACE._signal_handler(signal.SIGINT, lambda *a: None)(signal.SIGINT, None)
        TRACE.finish()
    """)
    assert r.returncode == 0
    assert any(r["event"] == "signal" for r in _recs(tmp_path / "trace.jsonl"))


def test_signal_other_thread_lock_does_not_block_handler(tmp_path):
    r = _run(tmp_path, """
        import threading, signal, time
        ready, release = threading.Event(), threading.Event()
        def hold():
            with TRACE._lock:
                ready.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        assert ready.wait(2)
        before = time.monotonic()
        TRACE._signal_handler(signal.SIGINT, lambda *a: release.set())(signal.SIGINT, None)
        assert time.monotonic() - before < 1
        thread.join(2)
        assert not thread.is_alive()
        TRACE.finish()
    """)
    assert r.returncode == 0


def test_real_signal_raise_keeps_keyboard_interrupt(tmp_path):
    r = _run(tmp_path, """
        import signal
        try:
            signal.raise_signal(signal.SIGINT)
        except KeyboardInterrupt:
            TRACE.mark("Exception(KeyboardInterrupt)", via="caught_interrupt")
            raise SystemExit(23)
    """)
    rows = _recs(tmp_path / "trace.jsonl")
    assert r.returncode == 23
    assert any(x["event"] == "signal" for x in rows)
    assert rows[-1]["exit_kind"] == "Exception(KeyboardInterrupt)"


def test_analysis_cli_unknown_returns_nonzero_without_cause_guess(tmp_path):
    p = tmp_path / "trace.jsonl"
    p.write_text(json.dumps(_event("start")) + "\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(PROJECT / "scripts" / "analyze_exit_trace.py"),
                        "--path", str(p), "--no-process-check", "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert r.returncode == 2
    s = json.loads(r.stdout)
    assert s["unknown"] and not s["missing_exit"]
    assert s["instances"][0]["cause"] == "undetermined"


def test_real_running_child_then_clean_exit_are_distinguished(tmp_path):
    import time
    trace = tmp_path / "live_child.jsonl"
    script = ("import sys\nfrom core.exit_trace import install\n"
              f"t=install({str(trace)!r})\n"
              "t.set_stage('sleep')\nsys.stdin.readline()\nt.finish()\n")
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "-B", "-c", script],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", cwd=str(PROJECT),
                            env=dict(os.environ, PYTHONPATH=str(PROJECT), PYTHONUTF8="1"))
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = an.load(trace)
            if any(r.get("event") == "stage" for r in rows):
                break
            time.sleep(0.02)
        first = an.summarize(rows)
        lives = an.probe_liveness(first["instances"])
        active = an.summarize(rows, lives)
        assert len(active["running"]) == 1 and not active["missing_exit"]
        assert active["running"][0]["last_stage"] == "sleep"
    finally:
        proc.communicate(input="finish\n", timeout=10)
    assert proc.returncode == 0
    rows = an.load(trace)
    final = an.summarize(rows, an.probe_liveness(an.summarize(rows)["instances"]))
    assert final["instances"][0]["status"] == "exit_recorded"
    assert not final["log_issues"] and not final["missing_exit"]


@pytest.mark.parametrize("failure", ["flush", "short_write"])
def test_flush_and_short_write_require_retry(tmp_path, monkeypatch, failure):
    import builtins
    t = _install(tmp_path)
    original = builtins.open

    class FailedIO:
        def __enter__(self):
            self.fh = original(t.path, "ab")
            return self
        def write(self, data):
            if failure == "short_write":
                self.fh.write(data[:20])
                return 20
            return self.fh.write(data)
        def flush(self):
            raise OSError("flush failure")
        def __exit__(self, *args):
            self.fh.close()

    with monkeypatch.context() as m:
        m.setattr(builtins, "open", lambda *a, **k: FailedIO())
        assert not t.mark("SystemExit(8)")
    t.finish()
    s = an.summarize(an.load(t.path))
    assert s["instances"][0]["exit_kind"] == "SystemExit(8)"
    assert t._exit_written


def test_v2_missing_run_id_is_not_treated_as_legacy():
    s = an.summarize([{"schema_version": 2, "event": "start", "pid": 123,
                       "started_at": "2026-11-10T04:30:00"}])
    assert s["total"] == 0 and s["log_issues"]


def test_duplicate_event_id_with_different_contents_blocks_green():
    s = an.summarize([_event("start", event_id="same"),
                      _event("exit", event_id="same", exit_kind="normal_return")])
    assert s["unknown"] and s["log_issues"]
    assert any(x["code"] == "conflicting_event_id" for x in s["log_issues"])
