# -*- coding: utf-8 -*-
"""星耀（AmazingData）链路失败分类 —— H44 分类结论的代码化。

背景（2026-10-19 D24，`live_20261019.log` 35 个失败块全解析）：四类失败各有
**不同的抛出点与阻塞特征**，此前只能看 traceback 人工分拣，统计成本高、易漏。
本模块给每类一个稳定的 `kind` / `stage`，日志里一行即可聚合。

| kind            | 抛出点                                   | 实测单轮阻塞 |
|-----------------|------------------------------------------|--------------|
| `login_timeout` | `data_fetch._login()` 线程 join 超时      | 中位 71s     |
| `login_error`   | `ad.login()` 抛异常                       | —            |
| `calendar_error`| `get_calendar()` 返 None / 抛 TypeError   | 中位 159s    |
| `kline_empty`   | `query_kline` 空返回（重登重试后仍空）    | 8–61s        |
| `sdk_error`     | 其余 SDK 异常（如 `Exception: 查询失败`） | 8–61s        |
| `fetch_timeout` | 整轮 wall-clock 上限（线程被放弃）        | 上限值       |
| `circuit_open`  | 熔断开闸期间快速失败（不调任何 SDK）      | ≈0（H34 三件套）|

⚠️ 新增 kind 前先在本文件 grep，避免同义异名（§6.9 第十六形态）。
⚠️ 所有异常都继承 `AdError(RuntimeError)`，`*_timeout` 额外继承 `TimeoutError`，
   保证既有 `except RuntimeError / TimeoutError` 语义不变。
"""
from __future__ import annotations

__all__ = [
    "AdError", "AdLoginTimeout", "AdLoginError", "AdCalendarError",
    "AdKlineEmpty", "AdSdkError", "AdFetchTimeout", "AdCircuitOpen",
    "classify", "format_failure_line", "KINDS",
]

KINDS = (
    "login_timeout", "login_error", "calendar_error",
    "kline_empty", "sdk_error", "fetch_timeout", "circuit_open",
)


class AdError(RuntimeError):
    """星耀链路错误基类：带 kind（可聚合）与 stage（出错环节）。"""

    kind = "unclassified"
    stage = "unknown"

    def __init__(self, message: str, *, stage: str | None = None,
                 kind: str | None = None):
        super().__init__(message)
        if stage is not None:
            self.stage = stage
        if kind is not None:
            self.kind = kind


class AdLoginTimeout(AdError, TimeoutError):
    """登录软超时：线程 join 超过 _LOGIN_TIMEOUT，SDK 仍未返回。"""

    kind = "login_timeout"


class AdLoginError(AdError):
    """登录抛异常（账号/密码/host 端口/网络）。"""

    kind = "login_error"


class AdCalendarError(AdError):
    """get_calendar() 返回 None，或内部抛 TypeError（SDK base_data.py:50）。"""

    kind = "calendar_error"


class AdKlineEmpty(AdError):
    """query_kline 返回空 / 缺 code（已清缓存重登重试一次仍空）。"""

    kind = "kline_empty"


class AdSdkError(AdError):
    """其余 SDK 异常（如 SDK 内部抛 `Exception: 查询失败`）。"""

    kind = "sdk_error"


class AdFetchTimeout(AdError, TimeoutError):
    """整轮硬超时：墙钟上限到点，工作线程被放弃（Python 无法强杀线程）。"""

    kind = "fetch_timeout"


class AdCircuitOpen(AdError):
    """熔断开闸：连续失败超阈值后快速失败，**不调用任何 SDK**。

    H34 三件套之一。目的不是省时间，而是不再新增"可能挂死的登录线程"
    （H37：AD 登录在原生层 abort）。继承 `RuntimeError`，既有
    `except RuntimeError` 语义不变；**不继承 TimeoutError** —— 它不是超时，
    也不想被历史 `except TimeoutError` 当成"再试一次"的信号。
    """

    kind = "circuit_open"


_FALLBACK_BY_TYPE = {
    "TimeoutError": "timeout",
    "RuntimeError": "runtime",
    "TypeError": "type_error",
}


def classify(exc: BaseException) -> tuple[str, str]:
    """返回 (kind, stage)。未包装的原始异常回落到类型名，绝不抛异常。"""
    kind = getattr(exc, "kind", None)
    stage = getattr(exc, "stage", None)
    if kind is None:
        kind = _FALLBACK_BY_TYPE.get(type(exc).__name__, "unclassified")
    if stage is None:
        stage = "unknown"
    return str(kind), str(stage)


def format_failure_line(exc: BaseException, elapsed: float) -> str:
    """一行结构化失败摘要（**不含换行**，避免破坏"按块计数"）。

    输出形如：
        [collector] 采集失败 class=calendar_error stage=calendar elapsed=158.2s
    """
    kind, stage = classify(exc)
    return (f"[collector] 采集失败 class={kind} stage={stage} "
            f"elapsed={float(elapsed):.1f}s")
