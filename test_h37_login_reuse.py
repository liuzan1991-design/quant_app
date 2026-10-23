# -*- coding: utf-8 -*-
r"""H37 待决登录线程复用 —— 核心不变量：任意时刻"已启动未结束"的 ad.login() ≤ 1。

依据：D:\Codex输出\H37实施稿_待决登录线程复用_20261022.md §6
背景：超时后旧线程卡在 tgw 原生层，旧代码下次再新起一个 ⇒ 线程累积
⇒ 26~44 个时进程原生层 abort（6 次实证：09-24×3 / 09-30 / 10-08 / 10-19）。
"""
import threading
import time

import pytest

import core.data_fetch as data_fetch
from core.ad_errors import AdLoginError, AdLoginTimeout

CODE = "300308"
DAY = "2026-10-23"

CRED = {"username": "secret-user", "password": "secret-pass",
        "host": "h", "port": 1}


@pytest.fixture(autouse=True)
def _fast_login(monkeypatch):
    """登录超时压到 0.3s（原 45s），凭证用假值，冷却关掉。

    冷却必须为 0：生产教训正是「10s 冷却在 45s 超时前无效，累积只降速未归零」；
    测试里保留冷却会把「摘掉复用」的缺陷掩盖成"被冷却挡住"（假绿）。
    """
    monkeypatch.setattr(data_fetch, "_LOGIN_TIMEOUT", 0.3)
    monkeypatch.setattr(data_fetch, "_LOGIN_FAIL_COOLDOWN", 0.0)
    monkeypatch.setattr(data_fetch, "load_credentials", lambda: dict(CRED))
    data_fetch._AD_CACHE.pop("ad", None)
    yield


def _alive_login_threads() -> int:
    return sum(1 for t in threading.enumerate()
               if t.name == "ad-login" and t.is_alive())


class _BlockingLogin:
    """login 阻塞在 gate 上；set_gate 后可选择性成功/抛错。"""

    def __init__(self, gate: threading.Event, outcome=None):
        self._gate = gate
        self._outcome = outcome  # None=成功；异常对象=抛出
        self.calls = 0

    def login(self, **kwargs):
        self.calls += 1
        self._gate.wait(timeout=10)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return None


# ---------------------------------------------------------------- 1 超时保留 pending
def test_timeout_keeps_pending(monkeypatch):
    """第一次登录超时 ⇒ 抛 AdLoginTimeout 且 pending 保留（线程仍活着）。"""
    gate = threading.Event()
    fake = _BlockingLogin(gate)
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: fake)

    with pytest.raises(AdLoginTimeout):
        data_fetch._login_impl()

    pending = data_fetch._SESSION["pending_login"]
    assert pending is not None, "超时后 pending 被清空（回到旧累积行为）"
    assert pending[0].is_alive(), "pending 线程已死但未读结果"
    assert _alive_login_threads() == 1
    gate.set()  # 清理：让线程退出


# ------------------------------------------------- 2 第二次调用不新起线程
def test_second_call_reuses_thread(monkeypatch):
    """pending 卡着时再次调用 ⇒ 不新起线程（计数不变），仍快速超时。"""
    gate = threading.Event()
    fake = _BlockingLogin(gate)
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: fake)

    with pytest.raises(AdLoginTimeout):
        data_fetch._login_impl()
    started = data_fetch._SESSION["login_threads_started"]

    t0 = time.monotonic()
    with pytest.raises(AdLoginTimeout):
        data_fetch._login_impl()
    elapsed = time.monotonic() - t0

    assert data_fetch._SESSION["login_threads_started"] == started == 1, \
        "第二次调用新起了登录线程（累积源头未消除）"
    assert elapsed < 1.0, f"复用路径耗时 {elapsed:.3f}s（应快速失败）"
    assert _alive_login_threads() == 1
    gate.set()


# ---------------------------------------------- 3 卡住线程最终成功 ⇒ 入缓存
def test_pending_thread_eventual_success(monkeypatch):
    """卡住的线程最终成功返回 ⇒ 下一次调用直接命中缓存，pending 清空。"""
    gate = threading.Event()
    fake = _BlockingLogin(gate)
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: fake)

    with pytest.raises(AdLoginTimeout):
        data_fetch._login_impl()

    gate.set()  # 线程随后完成并写入 result["ad"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and data_fetch._SESSION["pending_login"][0].is_alive():
        time.sleep(0.02)

    ad = data_fetch._login_impl()
    assert ad is fake, "复用路径未返回登录结果"
    assert data_fetch._AD_CACHE.get("ad") is fake, "结果未入缓存"
    assert data_fetch._SESSION["pending_login"] is None, "成功后 pending 未清空"
    assert data_fetch._SESSION["login_threads_started"] == 1


# ---------------------------------------------------- 4 线程抛异常 ⇒ 透传
def test_pending_thread_error(monkeypatch):
    """pending 线程最终抛异常 ⇒ 下一次调用抛 AdLoginError，pending 清空。"""
    gate = threading.Event()
    boom = RuntimeError("counter rejected")
    fake = _BlockingLogin(gate, outcome=boom)
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: fake)

    with pytest.raises(AdLoginTimeout):
        data_fetch._login_impl()

    gate.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and data_fetch._SESSION["pending_login"][0].is_alive():
        time.sleep(0.02)

    with pytest.raises(AdLoginError) as ei:
        data_fetch._login_impl()
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert data_fetch._SESSION["pending_login"] is None
    # 异常必须来自原 pending 线程的透传，不允许落空后新起第二个线程重试
    assert data_fetch._SESSION["login_threads_started"] == 1, \
        f"新起了 {data_fetch._SESSION['login_threads_started']} 个线程（透传失效，走了重试路径）"


# --------------------------------- 5 核心不变量：连续 10 次超时，线程恒 ≤ 1
def test_core_invariant_10_timeouts(monkeypatch):
    """连续 10 次超时 ⇒ 新起线程数恒 =1、存活 ad-login 线程恒 ≤1。

    本修复的唯一"累积源消失"证明；旧代码此场景会累积 10 个线程。
    """
    gate = threading.Event()
    fake = _BlockingLogin(gate)
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: fake)

    timeouts = 0
    for _ in range(10):
        try:
            data_fetch._login_impl()
        except AdLoginTimeout:
            timeouts += 1
        assert data_fetch._SESSION["login_threads_started"] <= 1, \
            f"第 {timeouts + 1} 次调用时线程数已达 {data_fetch._SESSION['login_threads_started']}"
        assert _alive_login_threads() <= 1

    assert timeouts == 10
    assert data_fetch._SESSION["login_threads_started"] == 1
    assert _alive_login_threads() == 1
    gate.set()


# ------------------------------------------------------ 6 并发 5 线程只起 1
def test_concurrent_callers_single_thread(monkeypatch):
    """5 线程并发调 _login() ⇒ 只起 1 个登录线程（其余复用/快速失败）。"""
    gate = threading.Event()
    fake = _BlockingLogin(gate)
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: fake)

    errors = []

    def _hit():
        try:
            data_fetch._login_impl()
        except AdLoginTimeout:
            errors.append("timeout")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_hit, daemon=True) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15.0)

    assert data_fetch._SESSION["login_threads_started"] == 1, \
        f"并发下新起 {data_fetch._SESSION['login_threads_started']} 个登录线程"
    assert _alive_login_threads() == 1
    gate.set()


# ------------------------------- 7 H37-§8：SystemExit 不逃逸 + 文案中立
def test_system_exit_caught_and_redacted(monkeypatch):
    """SDK sys.exit(0) ⇒ 捕获为 AdLoginError；文案不带"网络"字样、不含凭证。"""
    gate = threading.Event()

    class _ExitLogin(_BlockingLogin):
        def login(self, **kwargs):
            self.calls += 1
            raise SystemExit(0)   # tgw_login.py:102 实证行为

    fake = _ExitLogin(gate)
    monkeypatch.setattr(data_fetch, "_import_ad", lambda: fake)

    with pytest.raises(AdLoginError) as ei:
        data_fetch._login_impl()
    msg = str(ei.value)
    assert isinstance(ei.value.__cause__, SystemExit), "SystemExit 未被 BaseException 捕获"
    assert "网络" not in msg, f"文案仍带误导性网络归因：{msg}"
    assert "secret-user" not in msg and "secret-pass" not in msg, "异常文案泄露凭证"
    assert data_fetch._SESSION["pending_login"] is None
