# -*- coding: utf-8 -*-
"""星耀数智（AmazingData）1 分钟数据增量拉取，写入 APP 本地数据目录。"""
from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd

from .ad_silence import suppress_sdk_output
from .ad_errors import (  # noqa: F401  （部分名称在此 re-export，供 live_run / 工具脚本取用）
    AdCalendarError,
    AdCircuitOpen,
    AdError,
    AdFetchTimeout,
    AdKlineEmpty,
    AdLoginError,
    AdLoginTimeout,
    AdSdkError,
    classify,
    format_failure_line,
)

APP_DIR = Path(__file__).resolve().parents[1]
CRED_FILE = Path(r"D:\Codex输出\ad_credentials.json")
STD_COLS = ["time", "open", "high", "low", "close", "volume", "amount"]
_AD_CACHE = {}

# 登录软超时（秒）：断网时 ad.login() 会无限阻塞（第三方 SDK 不设超时），
# 必须在线程里 join 超时，否则整个采集进程卡死在登录、不抛异常、无 traceback。
_LOGIN_TIMEOUT = 45

# SDK 调用期的 socket 默认超时（秒）。
# 🚨 H44 实测根因之一：登录结束后 finally 把 setdefaulttimeout 恢复成 None，
#    于是后续 get_calendar()/query_kline() 全部无超时裸奔（TypeError 块实测
#    单轮阻塞 158~397 秒）。这里把它覆盖到**整个** fetch 过程，而不是只在登录期。
_SDK_SOCKET_TIMEOUT = 20

# 盘中单轮硬上限（秒）：socket 超时只管单次收发，管不住 SDK 内部重试循环，
# 因此再叠一层墙钟上限。仅 live 采集路径启用（market_collector.fetch_today），
# 历史批量补数不启用 —— 长区间拉取本就该慢，不该被硬砍。
LIVE_FETCH_TIMEOUT = 90

# ------------------------------------------------------------------ H34 会话治理
# 依据（28 份 live_*.log 全量统计，09-09~10-22，不是抽样）：
#   TimeoutError@_login           417 次（49%）
#   TypeError@get_calendar        360 次（42%）  ← H34 本体
#   RuntimeError@fetch_kline       71 次（8%，**唯一**有清缓存重登的路径）
# ⇒ 91% 的故障是"会话/连接不可用"，却只有 8% 那条路会重建。
# 三件套：① 日历缓存（每日 300~470 次调用压到 1 次）；② 被动重建（统一入口）；
#         ③ 熔断（限流，防"失败就重登"造出更多可能挂死的原生登录线程，H37）。
_AD_LOCK = threading.RLock()   # 登录单飞 + 缓存读写互斥（原先裸 dict 无锁 ⇒ 并发互踢）
_LOGIN_FAIL_COOLDOWN = 10.0    # 秒；登录失败后此时长内快速失败，避免线程排队串行登录

REBUILD_MIN_INTERVAL = 60.0    # 秒；正常轮节拍 30~48s，60s 保证重建后至少跑完一轮再看结果
REBUILD_DAILY_CAP = 20         # 每日重建上限；最坏日实测 197 块失败，不封顶会打出 197 个登录线程

BREAKER_FAIL_THRESHOLD = 5     # 连续失败阈值：游程 ≤5 占实测 64.3%（≤3 会误伤 40% 的自愈故障）
BREAKER_COOLDOWN = 180.0       # 秒；必须 > 实测单轮阻塞中位 158s / ~188s 簇形态
# 计入熔断游程的 kind：calendar_error 走重建（5 次就把可重建故障判"整日不可用"是误判），
# kline_empty 可能是休市/代码无效（H38 语境），同样不计。
_BREAKER_KINDS = ("login_timeout", "login_error", "sdk_error", "fetch_timeout")

_SESSION = {
    "day": "",
    "rebuild_count": 0,
    "rebuild_rejected": 0,
    "last_rebuild_at": 0.0,
    "breaker_state": "closed",     # closed / open / half_open
    "breaker_opens": 0,
    "breaker_fails": 0,
    "opened_at": 0.0,
    "half_open_inflight": False,
    "login_fail_at": 0.0,
}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _roll_session_day() -> None:
    """换日清零重建计数（熔断计数不跨日累计，避免第二天开盘就带闸）。"""
    today = _today()
    with _AD_LOCK:
        if _SESSION["day"] != today:
            _SESSION["day"] = today
            _SESSION["rebuild_count"] = 0
            _SESSION["rebuild_rejected"] = 0
            _SESSION["last_rebuild_at"] = 0.0
            _SESSION["breaker_fails"] = 0
            if _SESSION["breaker_state"] != "closed":
                _SESSION["breaker_state"] = "closed"
                _SESSION["half_open_inflight"] = False


def reset_session_state() -> None:
    """测试用：清日历缓存 + 全部治理计数（**不动** `_AD_CACHE["ad"]`）。

    不整块 clear 是为了不干扰 `test_h44_ad_failure.py` 里
    `monkeypatch.setattr(data_fetch, "_AD_CACHE", {})` + 手工塞 ad 的桩法。
    """
    with _AD_LOCK:
        _AD_CACHE.pop("calendar", None)
        _SESSION.update({
            "day": _today(), "rebuild_count": 0, "rebuild_rejected": 0,
            "last_rebuild_at": 0.0, "breaker_state": "closed", "breaker_opens": 0,
            "breaker_fails": 0, "opened_at": 0.0, "half_open_inflight": False,
            "login_fail_at": 0.0,
        })


def session_stats() -> dict:
    """供心跳/巡检聚合的可观测字段（H46 教训：缺字段 ⇒ "进程活着"被误读成"在干活"）。"""
    try:
        with _AD_LOCK:
            return {
                "breaker_state": _SESSION["breaker_state"],
                "breaker_opens": int(_SESSION["breaker_opens"]),
                "breaker_fails": int(_SESSION["breaker_fails"]),
                "rebuild_count": int(_SESSION["rebuild_count"]),
                "rebuild_rejected": int(_SESSION["rebuild_rejected"]),
            }
    except Exception:  # noqa: BLE001 —— 可观测性代码不得成为新的失败点
        return {}


def _rebuild_session(reason: str) -> bool:
    """被动重建：只清缓存，**不主动登录**，由下一次 fetch 在既有 45s 软超时下自然登录。

    主动登录要新起线程，多一个可能挂死的原生线程（H37 正是登录在原生层 abort）；
    复用既有路径 = 零新增风险，代价最多延后一轮（30s）。
    返回 False 表示被限流拒绝（调用方应直接抛原异常，不要硬重试）。
    """
    try:
        now = time.monotonic()
        with _AD_LOCK:
            if _SESSION["day"] != _today():
                _roll_session_day()
            if _SESSION["rebuild_count"] >= REBUILD_DAILY_CAP:
                _SESSION["rebuild_rejected"] += 1
                return False
            if now - _SESSION["last_rebuild_at"] < REBUILD_MIN_INTERVAL:
                _SESSION["rebuild_rejected"] += 1
                return False
            _AD_CACHE.clear()          # ad 引用 + calendar 缓存一起失效
            _SESSION["last_rebuild_at"] = now
            _SESSION["rebuild_count"] += 1
            return True
    except Exception:  # noqa: BLE001
        return False


def _breaker_before_call() -> None:
    """熔断门：open 且未到冷却 ⇒ 快速失败（不触任何 SDK）；冷却到期 ⇒ 放一个探针。"""
    now = time.monotonic()
    with _AD_LOCK:
        state = _SESSION["breaker_state"]
        if state == "open":
            if now - _SESSION["opened_at"] < BREAKER_COOLDOWN:
                left = BREAKER_COOLDOWN - (now - _SESSION["opened_at"])
                raise AdCircuitOpen(
                    f"星耀链路熔断中（连续失败 {_SESSION['breaker_fails']} 次，"
                    f"冷却剩余 {left:.0f}s），本轮不调用 SDK", stage="circuit")
            _SESSION["breaker_state"] = "half_open"
            _SESSION["half_open_inflight"] = True
            return
        if state == "half_open":
            if _SESSION["half_open_inflight"]:
                raise AdCircuitOpen(
                    "星耀链路半开探测进行中，本轮快速失败（并发探测会放大线程数）",
                    stage="circuit")
            _SESSION["half_open_inflight"] = True


def _breaker_on_success() -> None:
    try:
        with _AD_LOCK:
            _SESSION["breaker_fails"] = 0
            _SESSION["half_open_inflight"] = False
            _SESSION["breaker_state"] = "closed"
    except Exception:  # noqa: BLE001
        pass


def _breaker_on_failure(exc: BaseException) -> None:
    """按 kind 计数；只有 `_BREAKER_KINDS` 参与游程（calendar_error 走重建）。"""
    try:
        kind, _stage = classify(exc)
        if kind not in _BREAKER_KINDS:
            return
        now = time.monotonic()
        with _AD_LOCK:
            if _SESSION["half_open_inflight"]:
                _SESSION["half_open_inflight"] = False
                _SESSION["breaker_state"] = "open"
                _SESSION["opened_at"] = now
                _SESSION["breaker_opens"] += 1
                return
            _SESSION["breaker_fails"] += 1
            if (_SESSION["breaker_fails"] >= BREAKER_FAIL_THRESHOLD
                    and _SESSION["breaker_state"] != "open"):
                _SESSION["breaker_state"] = "open"
                _SESSION["opened_at"] = now
                _SESSION["breaker_opens"] += 1
    except Exception:  # noqa: BLE001
        pass


def code_to_ad(code: str) -> str:
    if "." in code:
        return code
    if code.startswith(("60", "68", "51", "56", "58", "50")):
        return f"{code}.SH"
    if code.startswith(("00", "30", "12", "15", "16", "18")):
        return f"{code}.SZ"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    return f"{code}.SH"


def load_credentials() -> dict:
    if not CRED_FILE.exists():
        raise FileNotFoundError("未找到星耀数智凭证文件 ad_credentials.json")
    return json.loads(CRED_FILE.read_text(encoding="utf-8"))


def _import_ad():
    """延迟导入 SDK。抽成函数仅为让测试注入替身（生产路径仍走真实 import，
    受 H19 离线守卫约束：pytest 里 import 会被拦，故不能拿它当注入点）。"""
    import AmazingData as ad
    return ad


def _login():
    """登录收口点（**H19 离线守卫在本函数上拦截**，测试里被整体替换）。

    实现放在 `_login_impl`：守卫装的是本函数名，若把 200 行实现直接写在这里，
    测试就无从验证锁/冷却本身（只能验证"守卫会拦"）。
    """
    return _login_impl()


def _login_impl():
    ad = _import_ad()
    # 单飞：整段"查缓存 → 登录 → 写缓存"持锁。
    # H34-1B：原先裸 dict 无锁 ⇒ 硬超时被放弃的线程与新一轮线程会并发登录，
    #         触发 AD 单点登录互踢（实测 TimeoutError/TypeError 交替出现）。
    with _AD_LOCK:
        if _AD_CACHE.get("ad") is not None:
            return _AD_CACHE["ad"]
        # 失败冷却：多线程排队等锁时若前一个刚失败，不要接着一个个去登
        # （每次 45s 且每次都会新增一个可能挂死的原生登录线程 = H37 的燃料）。
        since_fail = time.monotonic() - _SESSION["login_fail_at"]
        if _SESSION["login_fail_at"] and since_fail < _LOGIN_FAIL_COOLDOWN:
            raise AdLoginTimeout(
                f"星耀登录 {since_fail:.0f}s 前刚失败，冷却 "
                f"{_LOGIN_FAIL_COOLDOWN:.0f}s 内不重试", stage="login")
        cred = load_credentials()

        result: dict = {}

        def _worker():
            # 底层 socket 默认超时：兜底 connect/recv 无限阻塞（SDK 可能显式 timeout=None）。
            old = socket.getdefaulttimeout()
            socket.setdefaulttimeout(30)
            try:
                with suppress_sdk_output():
                    ad.login(username=cred["username"], password=cred["password"],
                             host=cred["host"], port=int(cred["port"]))
                result["ad"] = ad
            except Exception as exc:  # noqa: BLE001
                result["err"] = exc
            finally:
                socket.setdefaulttimeout(old)

        t = threading.Thread(target=_worker, daemon=True, name="ad-login")
        t.start()
        t.join(timeout=_LOGIN_TIMEOUT)
        if "err" in result:
            _SESSION["login_fail_at"] = time.monotonic()
            raise AdLoginError(
                f"星耀登录失败：{type(result['err']).__name__}: {result['err']}",
                stage="login") from result["err"]
        if "ad" not in result:
            _SESSION["login_fail_at"] = time.monotonic()
            raise AdLoginTimeout(
                f"星耀登录超时（>{_LOGIN_TIMEOUT}秒），疑似网络不通或服务器无响应",
                stage="login")
        _AD_CACHE["ad"] = result["ad"]
        _AD_CACHE.pop("calendar", None)   # 新会话 ⇒ 旧日历对象不再可信
        _SESSION["login_fail_at"] = 0.0
        return _AD_CACHE["ad"]


def _run_with_deadline(fn, timeout: float):
    """在守护线程里跑 fn，超过 timeout 抛 AdFetchTimeout。

    ⚠️ Python 无法强杀线程 ⇒ 超时后**放弃**该线程（daemon，进程退出即回收）。
    放弃的线程靠 `_SDK_SOCKET_TIMEOUT` 的 socket 默认超时自行退出，
    所以 socket 超时必须在外层设置（本函数不负责，避免被放弃线程回改全局值）。
    """
    box: dict = {}

    def _worker():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_worker, daemon=True, name="ad-fetch")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AdFetchTimeout(
            f"星耀采集整轮硬超时（>{timeout}秒）：SDK 无响应，本轮放弃",
            stage="deadline")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _get_calendar(ad):
    """取交易日历；**当日**缓存命中则完全不调 SDK。

    H34 ①：正常日 300~470 轮 ⇒ 原先每轮必调 `get_calendar()`，把 42% 的故障面
    暴露在一条没有重登的路径上。缓存后每日仅 1 次，且会话死亡会改成暴露在
    `query_kline` 上 —— 那条路径本来就有清缓存重登（等价于"死路并到活路"）。
    只缓存日历对象，**不缓存任何行情**；换日 / 会话重建 双失效。
    """
    today = _today()
    cached = _AD_CACHE.get("calendar")
    if isinstance(cached, dict) and cached.get("day") == today and cached.get("obj") is not None:
        return cached["obj"]
    try:
        base = ad.BaseData()
    except Exception as exc:  # noqa: BLE001
        raise AdSdkError(
            f"星耀 BaseData() 构造失败：{type(exc).__name__}: {exc}",
            stage="calendar") from exc
    try:
        calendar = base.get_calendar()
    except Exception as exc:  # noqa: BLE001
        # 实测形态：SDK base_data.py:50 'NoneType' object is not subscriptable
        raise AdCalendarError(
            f"星耀 get_calendar() 抛 {type(exc).__name__}: {exc}",
            stage="calendar") from exc
    if calendar is None:
        raise AdCalendarError(
            "星耀 get_calendar() 返回 None（SDK 未就绪或会话异常）",
            stage="calendar")
    try:
        with _AD_LOCK:
            _AD_CACHE["calendar"] = {"day": today, "obj": calendar}
    except Exception:  # noqa: BLE001 —— 缓存写失败只是退化，不得影响本次取数
        pass
    return calendar


def _fetch_kline_inner(code: str, begin: str, end: str,
                       period: str = "min1") -> pd.DataFrame:
    """真实拉取逻辑（不含超时包装）。每个环节按 H44 分类返错。"""
    ad_code = code_to_ad(code)
    b = int(begin.replace("-", ""))
    e = int(end.replace("-", ""))
    for attempt in range(2):
        ad = _login()
        try:
            calendar = _get_calendar(ad)
        except AdCalendarError:
            # H34 本体（占全部失败 42%）：原先在这里直接 raise，绕过了唯一的重登通道。
            # 现在统一走重建入口；重建成功则本轮再试一次（`_login` 会自然重登）。
            if attempt == 0 and _rebuild_session("calendar_error"):
                continue
            raise
        try:
            market = ad.MarketData(calendar)
            period_val = getattr(ad.constant.Period, period).value
        except Exception as exc:  # noqa: BLE001
            raise AdSdkError(
                f"星耀 MarketData 初始化失败：{type(exc).__name__}: {exc}",
                stage="query") from exc
        try:
            kline = market.query_kline([ad_code], begin_date=b, end_date=e,
                                       period=period_val)
        except Exception as exc:  # noqa: BLE001
            raise AdSdkError(
                f"星耀 query_kline 抛 {type(exc).__name__}: {exc}",
                stage="query") from exc
        if kline is not None and ad_code in kline and kline[ad_code] is not None:
            df = kline[ad_code].reset_index(drop=True)
            break
        # 连接可能被顶/失效：走统一重建入口（带 60s/20 次限流），下一轮自然重登
        _rebuild_session("kline_empty")
        if attempt == 1:
            raise AdKlineEmpty(
                f"星耀接口未返回 {ad_code} 的数据（可能账号连接被占用或代码无效，"
                f"请检查是否其他会话正在使用星耀）", stage="query")
    else:
        raise AdKlineEmpty(f"星耀接口未返回 {ad_code} 的数据", stage="query")
    time_col = next((c for c in ("kline_time", "time") if c in df.columns), None)
    if time_col is None:
        raise AdSdkError(f"K线返回中未找到时间列：{list(df.columns)}", stage="parse")
    df = df.rename(columns={time_col: "time"})
    for col in STD_COLS:
        if col not in df.columns:
            df[col] = 0
    return df[STD_COLS].copy()


def fetch_kline(code: str, begin: str, end: str, period: str = "min1",
                hard_timeout: Optional[float] = None) -> pd.DataFrame:
    """拉取 [begin, end]（YYYY-MM-DD）的K线数据，period 支持 min1/day 等，返回标准列 DataFrame。

    hard_timeout：
      - `None`（默认）：只加 socket 默认超时，不加上限 —— 历史批量补数走这条路，
        长区间本就该慢，不该被硬砍。
      - 数值：整轮 wall-clock 硬上限（含内部重登重试）。**盘中采集必须传**，
        否则 SDK 内部重试循环可把单轮拖到 158~397 秒（H44 实测）。

    socket 默认超时在外层设置：超时被放弃的工作线程不会回改全局值（否则会把
    外层刚设的值恢复成 None，重新变成无超时裸奔）。

    ⚠️ 硬超时触发时**故意不复原**全局 socket 超时：被放弃的工作线程仍在跑 SDK
    调用，若此刻把全局值恢复成 `None`，它立刻重新变成无限阻塞。代价是此后本进程
    的 socket 默认超时停在 `_SDK_SOCKET_TIMEOUT`（有界，比 `None` 安全）。

    H34 熔断（`_BREAKER_KINDS` 连续 5 次 ⇒ 开闸 180s ⇒ 单探针半开）：
    - 开闸期间**不触任何 SDK**，直接抛 `AdCircuitOpen`（elapsed≈0）；
    - 目的是不再新增"可能挂死的登录线程"（H37），不是省时间；
    - `calendar_error` / `kline_empty` 不计入游程（前者走重建，后者可能是休市）。
    """
    _roll_session_day()
    _breaker_before_call()      # 抛 AdCircuitOpen 时**尚未**改 socket 全局值
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_SDK_SOCKET_TIMEOUT)
    abandoned = False
    try:
        try:
            if hard_timeout is None:
                df = _fetch_kline_inner(code, begin, end, period)
            else:
                df = _run_with_deadline(
                    lambda: _fetch_kline_inner(code, begin, end, period), hard_timeout)
        except BaseException as exc:  # noqa: BLE001 —— 计数后原样抛出，不改语义
            _breaker_on_failure(exc)
            if getattr(exc, "kind", "") == "sdk_error":
                # 会话级 SDK 异常（BaseData/MarketData/query 构造失败）也可能只是会话坏了
                _rebuild_session("sdk_error")
            raise
        _breaker_on_success()
        return df
    except AdFetchTimeout:
        abandoned = True
        raise
    finally:
        if not abandoned:
            socket.setdefaulttimeout(old)


def incremental_update(code: str, start: str, end: str,
                       data_dir: Optional[Path] = None) -> Tuple[int, int, str]:
    """增量更新本地 CSV。已有数据只补"上次日期之后 + 最近 7 天覆盖"；无数据则全量拉。"""
    data_dir = Path(data_dir) if data_dir else APP_DIR / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f"stock_{code}_1m.csv"

    existing = None
    if target.exists():
        existing = pd.read_csv(target, parse_dates=["time"])

    begin_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end)
    if existing is not None and len(existing):
        first = existing["time"].min()
        last = existing["time"].max()
        in_range = ((existing["time"] >= begin_dt) &
                    (existing["time"] <= end_dt.replace(hour=15, minute=0))).sum()
        if (first <= begin_dt + pd.Timedelta(days=5)
                and last >= end_dt.replace(hour=15, minute=0)
                and in_range > 0):
            return 0, len(existing), f"本地数据已覆盖 {first:%Y-%m-%d} ~ {last:%Y-%m-%d}，无需更新"
        if end_dt < first:
            begin_dt = pd.Timestamp(start)      # 目标区间在已有数据之前：全量拉目标区间
        elif begin_dt > last:
            begin_dt = last - pd.Timedelta(days=7)  # 目标区间在已有数据之后：增量补缺口
        else:
            begin_dt = pd.Timestamp(start)      # 部分重叠：全量拉目标区间，保证完整

    new = fetch_kline(code, begin_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
    if not len(new):
        total = len(existing) if existing is not None else 0
        return 0, total, f"区间内没有新数据（{begin_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d}）"

    if existing is not None and len(existing):
        merged = pd.concat([existing, new], ignore_index=True)
    else:
        merged = new
    merged = merged.drop_duplicates(subset="time", keep="last").sort_values("time").reset_index(drop=True)
    merged.to_csv(target, index=False, encoding="utf-8-sig")
    return len(merged) - (len(existing) if existing is not None else 0), len(merged), \
        f"更新完成：新增 {len(merged) - (len(existing) if existing is not None else 0)} 行，共 {len(merged)} 行"
