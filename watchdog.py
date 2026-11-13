# -*- coding: utf-8 -*-
"""心跳看门狗 (W1)：兜住"盘中静默消失"

设计依据
========
背景：观察期已发生三次"盘中静默消失"（09-09 / 09-10 / 09-16）——进程无声消失、
日志干净、无异常，而计划任务的 RestartOnFailure **只对非零退出码生效**，对这类
"退出码 0 / 无痕迹"的死亡完全无效。心跳是唯一可靠的"我还活着"信号。

阈值标定（**从实际分布取，非估算**）
------------------------------------
数据源：`live_outputs/logs/live_*.log` 中 `[collector] 采集完成` 行的相邻间隔
（该行每轮循环写一次，等价于 heartbeat.updated_at 的刷新节奏）

| 日期   | 中位 | P99 | 最大正常间隔 |
|--------|------|-----|--------------|
| 09-16  | 51s  | 86s | **91s**      |
| 09-15  | 49s  | 68s | **74s**      |
| 09-14  | 40s  | 71s | **327s** ← 存在 5.5 分钟的正常间歇 |

取 3 倍余量：3 × 327s = 981s ≈ 16.4 分钟 → **阈值取 20 分钟**
（满足"阈值 ≥ 正常最大间隔 3 倍"的自定纪律，避免把"慢一轮"误判为"死亡"）

存活判据：用 `heartbeat.updated_at`（每轮循环必写，无论采集成功或失败）判定
"进程还在不在跑"。`last_success_at` **不参与动作**，只作陈旧度告警（H36）。

理由：网络中断时采集会连续失败，但循环仍在跑、updated_at 仍会刷新——那种情况
**不该重启**（重启解决不了网络问题，纯属扰动；2026-10-19 更实证了 5 次重启全败）。
但"不重启"不等于"不吭声"：当天正是因为只看 updated_at，采集停摆 33 分钟无人
发现。故新增 `_report_success_staleness()` 告警旁路，**动作面一行未改**。

三道防误判门槛
--------------
1. **时间窗**：仅在本地 04:35–10:30 生效（盘前到收盘缓冲结束），窗口外一律不动
2. **进程存在性**：若仍有 python 进程持有 live_run.py，判定为"卡死"而非"死亡"
   —— 此时直接 `schtasks /Run` 会因 IgnoreNew 静默失效，故 **2026-10-28 起改为「先 `taskkill` 再 /Run」**（H62）：
   当日实证进程在 04:35 后**零日志悬挂**、心跳停在 04:35，靠"进程恰好死了"才被 ALERT-DEAD 救到；
   若它挂着不死，就会重演 10-27 白等 6 小时。**只对"当日从未成功"的悬挂下手**，
   已有成功记录的悬挂仍只告警（防在收盘流程中强杀）。
3. **正常收盘识别**：若今日水位已达 14:59，说明已正常收盘，**不重启**

用法
----
    python watchdog.py            # 巡检一次（供计划任务每 5 分钟调用）
    python watchdog.py --dry-run  # 只判断不动作，用于验证
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from core.trading_calendar import BJ, beijing_today, is_trading_day
from core.alerts import clear_alert, raise_alert   # H36 告警送达
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# 测试用：允许用环境变量指向沙箱目录，避免测试污染真实状态
LIVE_DIR = Path(os.environ.get("QUANT_LIVE_DIR") or (APP_DIR / "live_outputs"))
HEARTBEAT = LIVE_DIR / "heartbeat.json"
WATCHDOG_LOG = LIVE_DIR / "logs" / "watchdog.log"
TASK_NAME = os.environ.get("QUANT_WATCHDOG_TASK", "QuantLiveRun")

# ---- 阈值（标定依据见模块 docstring）----
STALE_SECONDS = 20 * 60          # 心跳静默超过 20 分钟 → 判定不在跑
# 采集陈旧（last_success_at）**只用于告警，不触发动作**。标定见 H36 说明。
# 数据源：live_outputs/logs/live_*.log 全量「[collector] 采集完成」相邻间隔
# （24 个交易日、6015 个间隔，仅取本地 04:25–10:35 盘中段）：
#   P50=48s  P90=57s  P99=117s  P99.5=234s  正常日最大 695s（09-21）
# 取 900s = 正常最大的 1.3 倍、P99 的 7.7 倍：全样本触发 13 次（0.22%），
# **全部落在已知故障日**（09-09/10/11/24/28/30、10-08/09/13/19），正常日零触发。
SUCCESS_STALE_SECONDS = 15 * 60
# 进程缺失判据的心跳停刷下限：任务拉起通常 5~40 秒内就有新心跳，120 秒足够区分
# "正在拉起"与"已经死了"，避免把启动窗口误判成死亡而重复 schtasks /Run。
PROC_GONE_SECONDS = 120
WINDOW_START = (4, 35)           # 本地时间窗口起（盘前 5 分钟）
WINDOW_END = (10, 30)            # 本地时间窗口止（收盘缓冲结束后预留）
MARKET_CLOSE_HHMM = "14:59"      # 北京时间的收盘最后一根 bar

# ---- H60：僵尸采集的受控重启 ----
# 2026-10-27 实证：04:32 首枪 kline_empty，进程此后每天照刷 updated_at（age 恒小），
# 全天 422 次失败、0 成功，watchdog 只发 ALERT-STALE **不重启**，白丢 6 小时盘中数据，
# 直到 10:10 进程自己死掉才被拉起。故新增"活着但从未采到数"的受控重启：
ZOMBIE_MIN_AGE = 30 * 60         # 启动满 30 分钟仍 0 成功（≈60 轮，远超正常首轮耗时）
ZOMBIE_MIN_ERRORS = 30           # 且失败轮次 ≥30（排除"刚启动慢一轮"误杀）
ZOMBIE_MAX_RESTARTS = 3          # 每日受控重启上限（防重启风暴；今天是 1 次就救回）

# ---- H82（2026-11-13 立号）：startup 阶段卡死没有独立超时判据 ----
# 实证：11-13 07:55:25 判据 A 拉起实例 2 后，进程卡在 startup（只写过一次心跳，
#   status=starting、last_success=None），07:55→08:20 **白等 1498s**，直到 HANG 的
#   心跳静默阈值（1200s）才发现。代价：13:00–13:24 无实时采集，网格 20 笔买单被
#   stale_quote 拒掉，下午开盘 22 分钟策略失效。
# 正常冷启动实测（首行日志→首采完成）：274s / 273s / 283s / 334s。
#   取 600：对最慢样本留 266s 余量；不取 420——那只剩 86s，AD 登录变慢即误杀。
# 与 H62 分工：H62 看"心跳静默 1200s"（运行中卡死），本条看"startup 超 600s 无首采"。
STALL_STARTUP_MAX = 600          # startup 阶段上限（秒）：超时仍无首次采集 ⇒ 判卡死
STARTUP_ALERT = "startup_hang"   # 额度耗尽时的人工介入告警 kind

# ---- H79：「有过成功记录」之后的成功停摆（2026-11-10 立号）----
# 背景：11-10 09:56 券商断连后，AD SDK 会话卡死，每轮 90 秒硬超时、0 成功，
#       心跳照刷 3.5 小时，watchdog 走「已有成功记录 ⇒ 只告警」分支，全程不动作。
#       人工重启一次后 11.8 秒即恢复 ⇒ **这一类重启有效**，与 10-19 不同。
# 与 10-19 的分界（10-19 重启 5 次全败）：
#   10-19 = RSS 涨到 ~2GB 的内存泄漏型，换进程无效；
#   11-10 = RSS 107MB 的 SDK 会话卡死型，换进程有效。
#   ⇒ 故以 RSS 作分界，超限一律不动（避免制造僵尸）。
# 阈值纪律：**不新造时间阈值**，直接复用 SUCCESS_STALE_SECONDS(900s) ——
#   该值已由 24 个交易日 6015 个间隔标定（P99.5=234s、正常日最大 695s），
#   全样本仅 13 次触发且全部落在已知故障日、正常日零触发。
# 午休免疫：不靠时段判定（时段判错会误重启，比不重启更糟），
#   改要求**两轮检查间 collect_errors 必须新增** —— 午休期没有失败、增量为 0，
#   天然不触发；今天是从 0 涨到 229，必然触发。
# STALL_MIN_ERRORS 标定（2026-11-10 当日 230 次真实失败）：
#   失败轮次间隔中位 36s；按 watchdog 5 分钟周期切桶（42 个桶）——
#   每桶失败次数 中位 6、分布 {6:28, 5:10, 4:2, 3:1, 1:1}。
#   取 4：42 个桶中 40 个达标（95%），既不会因一轮慢就漏判，也不至于 1-2 次就动手。
SUCCESS_STALL_SECONDS = SUCCESS_STALE_SECONDS
STALL_MIN_ERRORS = 4             # 两轮检查间新增失败 ≥4 次（5 分钟窗口的 2/3 分位）
STALL_RSS_LIMIT_MB = 1500        # ≥1.5GB 判为 10-19 型内存泄漏，重启无效
# ---- 2026-11-10 用户审阅后的四条收紧（H78/H79）----
# 1) 额度：**尝试次数与恢复成功次数分开记账**；一次恢复失败即停止自动重试。
#    旧实现只有"重启次数"，kill 失败也扣额度、且失败后下一轮还会再试 ⇒ 反复重启。
STALL_MAX_ATTEMPTS = 2           # 每日自动尝试上限（正常路径 1 次成功即结束）
# ---- H81（2026-11-12 立号）：验证窗口与冷启动对齐 + pending 跨轮次 ----
# 实证：11-12 07:05:04 受控重启，新实例 07:05:25 启动、07:09:58 首次采集成功
#   ⇒ **冷启动 273s**；同日 04:30:12→04:34:46 冷启动 **274s**（两次高度一致）。
#   而旧 STALL_VERIFY_TIMEOUT=180s ⇒ 判据在首次成功**前 101 秒**就下 no_recovery，
#   属结构性假失败（重启其实成功），并因此锁死当日额度、留下永久告警。
# 口径：单轮窗口（180s）只决定"这一轮等多久"，**不再决定成败**；
#   成败由冷启动上限（420s）决定，到点仍无首次成功才算真失败。
#   420s = 实测最大 274s + 一轮采集约 40s + 余量，覆盖 3 个采集轮次仍有富余。
STALL_VERIFY_TIMEOUT = 180       # 单轮验证窗口（秒）；到点转 pending，跨轮次续验
STALL_VERIFY_POLL = 10           # 轮询间隔（秒）
STALL_COLD_START_MAX = 420       # 冷启动上限（秒）：超过仍无首次成功 ⇒ 判真失败
# 2) 失败关闭：RSS 未知、进程查询不可信，一律**不动作**（只告警），
#    否则"查不到"会被当成"没有风险"。
STALL_BLOCK_ALERT = "stall_blocked"   # 不确定而未动作时的告警 kind
STALL_DATA_DIR = Path(os.environ.get("QUANT_STALL_DATA_DIR") or (APP_DIR / "data" / "live"))


def log(msg: str) -> None:
    WATCHDOG_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _in_window(now: datetime) -> bool:
    # H38：交易日判定收敛到 core.trading_calendar（唯一入口）。
    # 原先这里自判 `weekday() >= 5`，与 live_run 各自漂移 ⇒ 改一处漏两处。
    if not is_trading_day(beijing_today(now)):
        return False
    return WINDOW_START <= (now.hour, now.minute) <= WINDOW_END


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


# 进程查询是否可信。本机实测 PowerShell 偶发"返回 0 + 空 stdout"（见 MEMORY 硬约束），
# 空输出会被解析成"没有 live_run 进程"。旧代码只 WARN 后返回 []，与"真的没进程"
# 无法区分 ⇒ 一旦拿它当重启判据就会误杀/误拉。故显式记录可信度，由调用方决定。
PROC_QUERY_TRUSTED = True


def _live_run_processes() -> list:
    """返回持有 live_run.py 的 python 进程 (pid, cmdline) 列表。

    查询失败或空 stdout 时返回 [] **并把 PROC_QUERY_TRUSTED 置 False**：
    此时 [] 只表示"查不到"，不表示"没有"。
    """
    global PROC_QUERY_TRUSTED
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        # 用 -EncodedCommand 传参：避免命令行内嵌引号被 Windows 转义破坏
        import base64
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60)
        out = (r.stdout or "").strip()
        if not out:
            PROC_QUERY_TRUSTED = False
            log(f"[WARN] 进程查询返回空 stdout（rc={r.returncode}, stderr={(r.stderr or '')[:200]!r}）"
                f" ⇒ 本次结果不可信，按「未知」处理")
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        PROC_QUERY_TRUSTED = True
        return [(d.get("ProcessId"), d.get("CommandLine") or "")
                for d in data if "live_run.py" in (d.get("CommandLine") or "")]
    except Exception as e:
        PROC_QUERY_TRUSTED = False
        log(f"[WARN] 进程查询失败: {e!r} | 原始输出片段: {(locals().get('out') or '')[:200]!r}"
            f" ⇒ 本次结果不可信，按「未知」处理")
        return []


def _today_watermark_reached_close() -> bool:
    """配置中全部启用组合的今日水位均收盘，才视为正常结束。"""
    try:
        from core import live_config
        cfg = live_config.load()
        combos = live_config.to_combos(cfg) if cfg is not None else [("300308", "grid_trade")]
        if not combos:
            return False
        day = datetime.now().date().isoformat()
        for code, sid in combos:
            # 与 live_match_loop._code_symbol 保持相同落盘契约。
            exchange = "SH" if code.startswith("68") else "SZ"
            wm = LIVE_DIR / f"{code}.{exchange}" / sid / "watermark.json"
            if not wm.exists():
                return False
            data = json.loads(wm.read_text(encoding="utf-8"))
            upto = data.get("processed_upto_by_day", {}).get(day)
            if upto is None:
                # 兼容 H47 之前的水位格式，但必须精确匹配今日时间。
                times = [t for t in data.get("processed_times", []) if t.startswith(day + "T")]
                upto = max(times, default="")
            if not (day + "T" + MARKET_CLOSE_HHMM <= upto < day + "T24:00"):
                return False
        return True
    except Exception as e:
        log(f"[WARN] 水位读取失败: {e!r}")
        return False


def _report_success_staleness(hb: dict, succ_age: float | None, suffix: str) -> None:
    """**H36**：循环还活着、但采集已经停摆 —— 只播报，不动作。

    盲区成因（2026-10-19 D24 实证）：H44 让 AD 查询通道间歇返空，进程每轮
    循环照跑、`updated_at` 每轮必刷，而 `last_success_at` 冻结在 09:34。
    看门狗按 `updated_at` 判"健康"⇒ 07:43 起停摆 33 分钟无人发现，
    直到 09:50 那次重启才救回。

    为什么只告警、不重启：
      当日 5 次人工重启**全败**（每次 RSS 涨到 ~2GB 后进程消失），只有
      09:50 那次成功 —— 说明"重启"不是可靠解药，盲目加频只会制造更多
      僵尸进程、并可能点燃 H37 的原生层 abort。先用告警攒一天
      "陈旧起点 → 是否自愈 → 恢复耗时"的样本，再决定阈值与次数上限；
      阈值与重启策略都必须有分布支撑，不能拍脑袋（本项目既有的定阈值纪律）。
    """
    if succ_age is None:
        if hb.get("status") == "running":
            msg = (f"进程已启动但从未采集成功（started_at={hb.get('started_at')}, "
                   f"collect_err={hb.get('collect_errors')}）→ 仅告警，不重启")
            log(f"[ALERT-STALE] {msg}{suffix}")
            # H36 送达：写 ALERT_ACTIVE.json + 桌面告警文件（不只写日志）
            raise_alert("collect_stale", msg)
        return
    if succ_age > SUCCESS_STALE_SECONDS:
        msg = (f"采集已停摆 {succ_age:.0f}s（> {SUCCESS_STALE_SECONDS}s）"
               f"last_success={hb.get('last_success_at')} "
               f"collect_err={hb.get('collect_errors')} status={hb.get('status')}"
               f" → 告警；是否重启由 H79 判据（含恢复验证）决定")
        log(f"[ALERT-STALE] 循环存活但**{msg}{suffix}")
        raise_alert("collect_stale", msg)
        return
    # 恢复正常 ⇒ 自动撤销告警（桌面文件一并删除，避免"狼来了"残留）
    clear_alert("collect_stale")


def _zombie_restart_decision(hb: dict, succ_age: float | None,
                             now: datetime | None = None) -> tuple[bool, str]:
    """判定"活着但从未采集成功"是否该做进程级重启（H60，纯函数，便于单测）。

    只在**从未成功**时出手（`succ_age is None`）。已有成功记录后的陈旧走 H36 告警旁路：
    2026-10-19 实证当日 5 次人工重启全败（RSS 涨到 ~2GB 后进程消失），
    那段故障不是"换进程能解"，盲目重启只会制造僵尸进程。
    """
    if succ_age is not None:
        return False, "已有成功记录 ⇒ 归 H36 陈旧告警，不做进程重启"
    if hb.get("status") != "running":
        return False, f"status={hb.get('status')}（非 running）"
    started = _parse_dt(hb.get("started_at") or "")
    if started is None:
        return False, "started_at 不可解析"
    age = ((now or datetime.now()) - started).total_seconds()
    if age < ZOMBIE_MIN_AGE:
        return False, f"启动仅 {age / 60:.0f} 分钟（< {ZOMBIE_MIN_AGE / 60:.0f}）"
    errs = int(hb.get("collect_errors") or 0)
    if errs < ZOMBIE_MIN_ERRORS:
        return False, f"失败仅 {errs} 次（< {ZOMBIE_MIN_ERRORS}）"
    return True, f"启动 {age / 60:.0f} 分钟 0 成功、失败 {errs} 次"


def _stall_state_path(day: str) -> Path:
    return WATCHDOG_LOG.parent / f"watchdog_stall_{str(day).replace('-', '')}.json"


def _prev_collect_errors(day: str) -> int | None:
    try:
        p = _stall_state_path(day)
        if p.exists():
            return int((json.loads(p.read_text(encoding="utf-8")) or {}).get("collect_errors"))
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] 停摆状态读取失败: {e!r}")
    return None


def _save_collect_errors(day: str, current: int) -> None:
    try:
        p = _stall_state_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"collect_errors": current,
                                 "checked_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] 停摆状态写入失败: {e!r}")


def _error_delta(day: str, current: int) -> tuple[int, int | None]:
    """返回 (两轮间新增失败数, 上轮值)。进程重启导致计数归零时，按当日累计计。"""
    prev = _prev_collect_errors(day)
    if prev is None:
        return current, None
    if current < prev:
        return current, prev          # 进程已重启，计数归零
    return current - prev, prev


def _success_stall_decision(hb: dict, succ_age: float | None, err_delta: int,
                            rss_mb: float | None, water_closed: bool = False,
                            *, proc_query_trusted: bool = True,
                            proc_count: int = 1) -> tuple[bool, str]:
    """H79 纯函数：有过成功记录、但成功已长时间停摆 ⇒ 是否受控重启。

    只在「有成功记录」时走这里；「从未成功」归 H60 僵尸路径，二者互斥。

    **失败关闭**（用户 2026-11-10 口径）：RSS 未知、进程查询不可信、进程列表为空
    都不允许动作。旧实现把这三种"未知"当"没问题"放行，会起双实例或空转。
    """
    if succ_age is None:
        return False, "从未成功（归 H60 僵尸路径，不走本判据）"
    if succ_age < SUCCESS_STALL_SECONDS:
        return False, f"上次成功 {succ_age / 60:.0f} 分钟前（< {SUCCESS_STALL_SECONDS // 60}）"
    if err_delta < STALL_MIN_ERRORS:
        return False, f"近轮新增失败仅 {err_delta} 次（< {STALL_MIN_ERRORS}）⇒ 非采集失败，不动"
    if water_closed:
        return False, "今日水位已达收盘，属正常结束"
    if not proc_query_trusted:
        return False, "进程查询不可信（空 stdout / 查询失败）⇒ 失败关闭：不杀也不拉"
    if proc_count <= 0:
        return False, "无 live_run 进程（归判据 A / H60，不在此处重启）"
    if rss_mb is None:
        return False, "RSS 未知 ⇒ 失败关闭（无法排除 10-19 内存泄漏型）"
    if rss_mb >= STALL_RSS_LIMIT_MB:
        return False, (f"RSS {rss_mb:.0f}MB ≥ {STALL_RSS_LIMIT_MB}MB ⇒ 10-19 内存泄漏型，"
                       f"换进程无效（H60 反例）")
    return True, (f"{succ_age / 60:.0f} 分钟无成功、近轮新增失败 {err_delta} 次、"
                  f"RSS {rss_mb:.0f}MB、进程 {proc_count} 个")


# ---- H79：尝试/成功分开记账 + 三重恢复验证（2026-11-10 收紧）----

def _stall_ledger_path(day: str) -> Path:
    return WATCHDOG_LOG.parent / f"watchdog_stall_ledger_{str(day).replace('-', '')}.json"


def _load_stall_ledger(day: str) -> dict:
    """当日 H79 账本：attempts（动作尝试）/ successes（恢复成功）/ stop_reason。"""
    base = {"attempts": 0, "successes": 0, "last_attempt_at": None,
            "last_outcome": None, "stop_reason": None, "detail": None}
    try:
        p = _stall_ledger_path(day)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8")) or {}
            base.update(data)
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] H79 账本读取失败: {e!r}")
    return base


def _save_stall_ledger(day: str, **fields) -> dict:
    led = _load_stall_ledger(day)
    led.update(fields)
    try:
        p = _stall_ledger_path(day)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] H79 账本写入失败: {e!r}")
    return led


def _stall_gate(led: dict) -> tuple[bool, str]:
    """是否允许再动手。一次恢复失败 ⇒ 当日停止自动重试（不是靠"额度没用完"）。"""
    if led.get("stop_reason"):
        return False, f"已停止自动重试：{led['stop_reason']}"
    if int(led.get("attempts") or 0) >= STALL_MAX_ATTEMPTS:
        return False, f"今日尝试已达上限 {led['attempts']}/{STALL_MAX_ATTEMPTS}"
    return True, ""


def _read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _combos() -> list[tuple[str, str]]:
    try:
        from core import live_config
        cfg = live_config.load()
        return list(live_config.to_combos(cfg)) if cfg is not None else [("300308", "grid_trade")]
    except Exception:  # noqa: BLE001
        return []


def _recovery_snapshot(day: str | None = None) -> dict:
    """恢复验证用的现场快照：进程身份 + last_success_at + CSV 最后一根 bar。"""
    from core import data_freshness

    day = day or f"{datetime.now():%Y-%m-%d}"
    hb = _read_json(HEARTBEAT)
    pids = None
    for _try in range(2):                      # 查询抖动重试一次，仍不可信才判"未知"
        pids = [p[0] for p in _live_run_processes()]
        if PROC_QUERY_TRUSTED:
            break
        _sleep(2)
    if not PROC_QUERY_TRUSTED:
        pids = None
    last_bar = None
    for code, _sid in _combos() or [("300308", "grid_trade")]:
        t, _n = data_freshness.scan_day_bars(
            STALL_DATA_DIR / f"stock_{code}_1m.csv", day)
        if t is not None and (last_bar is None or t > last_bar):
            last_bar = t
    return {
        "pids": pids,
        "started_at": hb.get("started_at"),
        "last_success_at": hb.get("last_success_at"),
        "last_bar_at": last_bar.isoformat(timespec="seconds") if last_bar else None,
    }


def _bj_now(now: datetime | None = None) -> datetime:
    """本机时刻 → 北京时间（naive）。本机 GMT+3，**不能直接拿钟点比交易时段**。"""
    now = now or datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()          # naive 按本机时区解释（与 beijing_today 同口径）
    return now.astimezone(BJ).replace(tzinfo=None)


def _session_now(now: datetime | None = None) -> str:
    """当前北京时段；复用 data_freshness 唯一实现，取不到时按盘中从严处理。"""
    try:
        from core import data_freshness
        return data_freshness.session_of(_bj_now(now))
    except Exception:  # noqa: BLE001 —— 判不出时段就按交易时段，宁可严不可松
        return "in_session"


def _recovery_verdict(prev: dict, cur: dict, attempt_ts: datetime | None,
                      now: datetime | None = None, session: str | None = None
                      ) -> tuple[str, str]:
    """三重验证，返回三态：``recovered`` / ``pending`` / ``failed``。

    H81（用户 2026-11-12 口径）相对旧版的两处改动：

    1. **冷启动期内不判死**。旧版只有 bool，180s 窗口一到就判 no_recovery；
       而实测冷启动 273~274s（两次），判据必然在首次成功前下结论。
       现在「新实例还没上线 / 还没首次成功」返回 ``pending``，由下一轮续验；
       只有超过 ``STALL_COLD_START_MAX``(420s) 仍无首次成功才 ``failed``。
    2. **非交易时段放宽水位判据**。午休（11:30–13:00）与盘前盘后本来就不产 bar，
       要求"CSV 水位推进"等于永不通过（11-12 07:08 的假失败里，这一条同样会拦）。
       非交易时段改要求"水位不倒退"，恢复信号以 ``last_success_at`` 前进为准。

    **不放宽的硬边界**：进程列表不可信、旧 PID 仍存活 —— 任何时点都直接 failed，
    这类是"双实例写同一 CSV"的风险，不能用等待换时间。

    Args:
        prev: 动作前快照
        cur: 当前快照
        attempt_ts: 发起重启的时刻（本机 naive）
        now: 判定时刻（本机 naive；测试注入用）
        session: 北京时段；None ⇒ 按当前时刻算
    """
    now = now or datetime.now()
    session = session or _session_now(now)
    elapsed = (now - attempt_ts).total_seconds() if attempt_ts else None
    if elapsed is not None and elapsed < -60:
        # 时钟回拨 / 账本来自未来：不能靠"再等等"把额度无限挂住
        return "failed", f"尝试时刻在未来（{attempt_ts}），判定时刻 {now} ⇒ 用时为负，拒绝续验"
    over_cold = elapsed is None or elapsed > STALL_COLD_START_MAX
    waiting = "failed" if over_cold else "pending"
    waited = f"{elapsed:.0f}s/{STALL_COLD_START_MAX}s" if elapsed is not None else "N/A"

    # ① 硬边界：查不到进程 / 旧 PID 还在 —— 任何时点都不放行
    if cur.get("pids") is None:
        return "failed", "进程列表不可信 ⇒ 无法确认新实例身份"
    old_pids = sorted(set(cur.get("pids") or []) & set(prev.get("pids") or []))
    if old_pids:
        return "failed", f"旧 PID 仍存活 {old_pids}"

    # ② 新实例身份：started_at 必须不早于本次尝试（容差 2s：心跳截断到秒）
    if attempt_ts is None:
        return "failed", "缺少尝试时刻 ⇒ 无法确认新实例身份"
    st = _parse_dt(cur.get("started_at") or "")
    if st is None or st < attempt_ts - timedelta(seconds=2):
        return waiting, (f"新实例尚未上线（started_at={cur.get('started_at')} < "
                         f"尝试时刻 {attempt_ts:%H:%M:%S}，已等 {waited}）")

    # ③ last_success_at 前进（新实例必须真的采到一次）
    ps = _parse_dt(prev.get("last_success_at") or "")
    cs = _parse_dt(cur.get("last_success_at") or "")
    if cs is None or (ps is not None and cs <= ps):
        return waiting, (f"新实例尚未采集成功（last_success_at "
                         f"{prev.get('last_success_at')} → {cur.get('last_success_at')}，"
                         f"已等 {waited}）")

    # ④ 行情水位
    pb = _parse_dt(prev.get("last_bar_at") or "")
    cb = _parse_dt(cur.get("last_bar_at") or "")
    if session == "in_session":
        if cb is None or (pb is not None and cb <= pb):
            return "failed", (f"CSV 水位未推进（{prev.get('last_bar_at')} → "
                              f"{cur.get('last_bar_at')}）")
        bar_note = f"水位 {cur.get('last_bar_at')}"
    else:
        # 午休 / 盘前 / 盘后：本来不产 bar，只要不倒退即视为一致
        if pb is not None and (cb is None or cb < pb):
            return "failed", (f"非交易时段（{session}）水位倒退（{prev.get('last_bar_at')} → "
                              f"{cur.get('last_bar_at')}）")
        bar_note = f"非交易时段（{session}）水位不变属正常（{cur.get('last_bar_at')}）"

    return "recovered", (f"实例 {sorted(cur['pids'])}、"
                         f"last_success {cur.get('last_success_at')}、{bar_note}")


def _verify_recovery(prev: dict, attempt_ts: datetime | None,
                     timeout: int | None = None, poll: int | None = None,
                     now_fn=None) -> tuple[str, str]:
    """轮询到「有结论」或单轮窗口到点。

    H81：单轮窗口（默认 180s）**只决定这一轮等多久**，不再决定成败。
    到点仍 pending ⇒ 返回 pending，由调用方落账本、下一轮接着验。
    """
    timeout = STALL_VERIFY_TIMEOUT if timeout is None else timeout
    poll = STALL_VERIFY_POLL if poll is None else poll
    deadline = time.monotonic() + timeout
    detail = "未取到现场"
    while True:
        cur = _recovery_snapshot()
        status, detail = _recovery_verdict(
            prev, cur, attempt_ts, now=(now_fn() if now_fn else None))
        if status != "pending":
            return status, detail
        if time.monotonic() >= deadline:
            return "pending", f"{detail} ⇒ 本轮窗口 {timeout}s 到点，转下一轮续验"
        _sleep(poll)


def _live_run_rss_mb(procs: list) -> float | None:
    """所有 live_run 进程 RSS 合计（MB）。查不到返回 None（判据按"未知"放行）。"""
    if not procs:
        return None
    try:
        import psutil
        total = 0
        for pid, _cmd in procs:
            if pid is None:
                continue
            try:
                total += psutil.Process(int(pid)).memory_info().rss
            except Exception:  # noqa: BLE001  进程已退出
                continue
        return total / 1024 / 1024 if total else None
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] RSS 查询失败: {e!r}")
        return None


def _maybe_restart_stall(hb: dict, succ_age: float | None, suffix: str = "") -> bool:
    """H79：成功停摆 + 错误累积 ⇒ 受控重启，并**验证数据真的恢复**。

    与旧版的区别（用户 2026-11-10 四条收紧）：
    - 尝试次数 / 恢复成功次数分开记账；一次恢复失败即停，不再"失败也继续试"；
    - RSS 未知、进程查询不可信 ⇒ 失败关闭（只告警）；
    - 恢复判据 = 新实例身份 + last_success_at 前进 + CSV 水位推进，三者全中；
    - 失败会把 stop_reason 写进账本，当日不再自动重试。

    H81（用户 2026-11-12 口径）在其上补两条：
    - **pending 跨轮次**：冷启动 273s > 单轮窗口 180s，本轮验不完就落账本 pending，
      下一轮接着验，不重复 kill / 不重复消耗额度；
    - **非交易时段放宽水位**：由 `_recovery_verdict` 按时段分支处理。
    """
    day = str(hb.get("date") or f"{datetime.now():%Y-%m-%d}")
    led = _load_stall_ledger(day)

    # ---- H81 首步：账本里已有 pending ⇒ 只续验，不再杀/拉/扣额度 ----
    pend = led.get("pending")
    if isinstance(pend, dict) and pend.get("attempt_at"):
        prev = pend.get("prev") or {}
        at = _parse_dt(pend.get("attempt_at") or "")
        status, detail = _verify_recovery(prev, at)
        if status == "recovered":
            _save_stall_ledger(day, successes=int(led.get("successes") or 0) + 1,
                               last_outcome="recovered", stop_reason=None,
                               pending=None, detail=detail)
            log(f"[动作] ✅ 恢复验证通过（跨轮次续验）：{detail}{suffix}")
            clear_alert(STALL_BLOCK_ALERT)
            clear_alert("stall_no_recovery")   # H81：假失败留下的告警必须一并撤掉
            return True
        if status == "failed":
            _save_stall_ledger(day, pending=None)
            _fail_stall(day, "no_recovery", detail)
            return False
        _save_stall_ledger(day, pending={**pend, "checked_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"},
                           detail=detail)
        log(f"[判定] 恢复验证 pending（跨轮次续验中）：{detail}{suffix}")
        return False

    current = int(hb.get("collect_errors") or 0)
    delta, prev = _error_delta(day, current)
    _save_collect_errors(day, current)

    water_closed = _today_watermark_reached_close()
    procs = _live_run_processes()
    trusted = PROC_QUERY_TRUSTED
    rss = _live_run_rss_mb(procs) if trusted else None
    need, reason = _success_stall_decision(
        hb, succ_age, delta, rss, water_closed,
        proc_query_trusted=trusted, proc_count=len(procs))
    if not need:
        log(f"[判定] 成功停摆不重启：{reason}{suffix}")
        if ("失败关闭" in reason) or ("不可信" in reason):
            # 不确定不等于安全：必须让人知道"看门狗这轮没能力判断"
            raise_alert(STALL_BLOCK_ALERT, f"H79 未动作（{reason}）", level="alert")
        return False

    allowed, why = _stall_gate(led)
    if not allowed:
        log(f"[ALERT-STALL] {reason} → {why}{suffix}")
        return False

    # 动作前现场（用于恢复比对；进程列表不可信时此处已不会走到）
    prev_state = _recovery_snapshot(day)
    attempt_ts = datetime.now()
    n_attempt = int(led.get("attempts") or 0) + 1
    _save_stall_ledger(day, attempts=n_attempt,
                       last_attempt_at=attempt_ts.isoformat(timespec="seconds"),
                       last_outcome="pending", detail=reason)

    log(f"[ALERT-STALL] {reason} → 上次成功 {hb.get('last_success_at')}、"
        f"累计失败 {current} 次（上轮 {prev}）⇒ 受控重启"
        f"（今日第 {n_attempt}/{STALL_MAX_ATTEMPTS} 次尝试）{suffix}")

    if not _kill_live_run(procs):
        _fail_stall(day, "kill_failed", "部分进程未能终止，放弃本次重启")
        return False
    _sleep(3)
    left = _live_run_processes()
    if left:
        _fail_stall(day, "kill_failed", f"仍残留进程 {[p[0] for p in left]}")
        return False

    _restart(f"成功停摆：{reason}", wait=False)
    status, detail = _verify_recovery(prev_state, attempt_ts)
    if status == "recovered":
        _save_stall_ledger(day, successes=int(led.get("successes") or 0) + 1,
                           last_outcome="recovered", stop_reason=None,
                           pending=None, detail=detail)
        log(f"[动作] ✅ 恢复验证通过：{detail}{suffix}")
        clear_alert(STALL_BLOCK_ALERT)
        clear_alert("stall_no_recovery")
        return True

    if status == "pending":
        # H81：冷启动未走完 ⇒ 落 pending，下一轮续验；**不判失败、不锁当日额度**
        _save_stall_ledger(day, last_outcome="pending",
                           pending={"attempt_at": attempt_ts.isoformat(timespec="seconds"),
                                    "prev": prev_state,
                                    "created_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"},
                           detail=detail)
        log(f"[判定] 恢复验证 pending（待下一轮续验）：{detail}{suffix}")
        return False

    _save_stall_ledger(day, pending=None)
    _fail_stall(day, "no_recovery", detail)
    return False


def _fail_stall(day: str, outcome: str, detail: str) -> None:
    """恢复失败：写账本 + 停止当日自动重试 + 告警（不静默）。"""
    led = _load_stall_ledger(day)
    _save_stall_ledger(day, last_outcome=outcome, pending=None,
                       stop_reason=f"{outcome}：{detail}",
                       failures=int(led.get("failures") or 0) + 1, detail=detail)
    log(f"[ALERT-STALL] ❌ 恢复未验证（{outcome}）：{detail} ⇒ 今日停止自动重试，需人工介入")
    raise_alert("stall_no_recovery",
                f"H79 自动恢复失败（{outcome}）：{detail}；已停止当日自动重试", level="alert")


def _restart_budget_path(day: str) -> Path:
    return WATCHDOG_LOG.parent / f"watchdog_restarts_{str(day).replace('-', '')}.json"


def _restart_budget(day: str) -> int:
    try:
        p = _restart_budget_path(day)
        if p.exists():
            return int((json.loads(p.read_text(encoding="utf-8")) or {}).get("count") or 0)
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] 重启额度读取失败: {e!r}")
    return 0


def _bump_restart_budget(day: str, reason: str) -> int:
    p = _restart_budget_path(day)
    used = _restart_budget(day) + 1
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"count": used, "last_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
                                 "reason": reason}, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"[WARN] 重启额度写入失败: {e!r}")
    return used


def _sleep(seconds: float) -> None:
    """单点 sleep（测试可替换，避免用例真等）。"""
    import time
    time.sleep(seconds)


def _kill_live_run(procs: list) -> bool:
    """逐个强杀 live_run 进程。返回是否全部返回码为 0。"""
    ok = True
    for pid, _cmd in procs:
        if pid is None:
            ok = False
            continue
        try:
            r = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=30)
            log(f"[动作] taskkill {pid} 返回码={r.returncode}")
            ok = ok and r.returncode == 0
        except Exception as e:  # noqa: BLE001
            log(f"[ERROR] taskkill {pid} 失败: {e!r}")
            ok = False
    return ok


def _maybe_restart_zombie(hb: dict, succ_age: float | None, suffix: str = "") -> bool:
    """H60：活着但从未采到数 ⇒ 杀进程 + schtasks 拉起。带每日额度与残留校验。"""
    need, reason = _zombie_restart_decision(hb, succ_age)
    if not need:
        log(f"[判定] 僵尸重启不触发：{reason}{suffix}")
        return False

    day = str(hb.get("date") or f"{datetime.now():%Y-%m-%d}")
    used = _restart_budget(day)
    if used >= ZOMBIE_MAX_RESTARTS:
        log(f"[ALERT-ZOMBIE] {reason} → 今日受控重启已用 {used}/{ZOMBIE_MAX_RESTARTS}，"
            f"不再重启{suffix}")
        return False

    procs = _live_run_processes()
    log(f"[ALERT-ZOMBIE] {reason} → 进程 {[p[0] for p in procs] or '无'} 空转，"
        f"执行受控重启（今日第 {used + 1}/{ZOMBIE_MAX_RESTARTS} 次）{suffix}")
    _bump_restart_budget(day, reason)

    if procs:
        if not _kill_live_run(procs):
            log("[ERROR] 部分进程未能终止 ⇒ 放弃本次重启（双实例会互相覆盖同一 CSV）")
            return False
        _sleep(3)
        left = _live_run_processes()
        if left:
            log(f"[ERROR] 仍残留进程 {[p[0] for p in left]} ⇒ 放弃重启（双实例风险）")
            return False

    _restart(f"僵尸采集：{reason}")
    return True


def _maybe_restart_hang(hb: dict, succ_age: float | None, procs: list,
                        suffix: str = "", dry: bool = False) -> bool:
    """H62：心跳静默但**进程还活着**（悬挂）⇒ taskkill + 重启。

    当日实证（2026-10-28）：04:35:19 之后进程**零日志**悬挂，心跳停在 04:35；
    05:00 因"进程恰好死了"被 ALERT-DEAD 拉起 —— 换句话说，**这一天是靠运气救回来的**。
    若它挂着不死，就是 10-27 那种"白等 6 小时"（当时 HANG 分支只告警）。

    保守边界：**只对"当日从未成功"的悬挂下手**。已有成功记录的悬挂可能是收盘流程未走完，
    强杀会留下半截状态（摘要/水位写一半），故维持"只告警、需人工"。
    """
    if not procs:
        log(f"[判定] HANG 重启不触发：无 live_run 进程（走死亡分支）{suffix}")
        return False
    if succ_age is not None:
        log(f"[ALERT-HANG] 进程 {[p[0] for p in procs]} 存活但心跳静默，"
            f"且当日已有成功记录（last_success={hb.get('last_success_at')}）"
            f"⇒ 不自动重启，需人工判断{suffix}")
        return False
    if dry:
        log(f"[判定] HANG 重启被 dry-run 抑制（进程 {[p[0] for p in procs]} 悬挂）{suffix}")
        return False

    day = str(hb.get("date") or f"{datetime.now():%Y-%m-%d}")
    used = _restart_budget(day)
    if used >= ZOMBIE_MAX_RESTARTS:
        log(f"[ALERT-HANG] 悬挂重启：今日受控重启已用 {used}/{ZOMBIE_MAX_RESTARTS}，"
            f"不再重启{suffix}")
        return False

    log(f"[ALERT-HANG] 进程 {[p[0] for p in procs]} 存活但当日 0 成功 ⇒ "
        f"执行受控重启（今日第 {used + 1}/{ZOMBIE_MAX_RESTARTS} 次）{suffix}")
    _bump_restart_budget(day, "心跳静默但进程存活且当日 0 成功")

    if not _kill_live_run(procs):
        log("[ERROR] 部分进程未能终止 ⇒ 放弃本次重启（双实例会互相覆盖同一 CSV）")
        return False
    _sleep(3)
    left = _live_run_processes()
    if left:
        log(f"[ERROR] 仍残留进程 {[p[0] for p in left]} ⇒ 放弃重启（双实例风险）")
        return False

    _restart("悬挂：心跳静默但进程存活且当日 0 成功")
    return True


def _startup_hang_decision(hb: dict, procs: list, now: datetime | None = None
                           ) -> tuple[bool, str]:
    """H82：进程在、当日从未采到数、启动已超上限 ⇒ 判 startup 卡死。

    只处理 ``status=starting`` 且 ``last_success_at`` 为空的情形：
    已在采数的进程归 H62/H79，本条不越界。

    失败关闭：进程查询不可信（procs 为空）不动手、缺 started_at 不动手、
    started_at 在未来（时钟回拨）不动手——这几条都不许用"再等等"糊过去。
    """
    if not procs:
        if not PROC_QUERY_TRUSTED:
            # 查不到 ≠ 没有：本机 PowerShell 偶发"exit 0 + 空输出"，
            # 这种时候必须失败关闭，不能当成"进程已经没了"。
            return False, "进程查询不可信 ⇒ 不动手（失败关闭）"
        return False, "无 live_run 进程（归判据 A）"
    status = str(hb.get("status") or "")
    if status != "starting":
        return False, f"status={status or '空'}（非 starting）"
    if hb.get("last_success_at"):
        return False, f"已有成功记录（{hb['last_success_at']}）⇒ 归 H62/H79"
    st = _parse_dt(hb.get("started_at") or "")
    if st is None:
        return False, "缺少 started_at ⇒ 无法判断启动时长"
    now = now or datetime.now()
    elapsed = (now - st).total_seconds()
    if elapsed < 0:
        return False, f"started_at 在未来（{hb.get('started_at')}）⇒ 时钟异常，不动手"
    if elapsed < STALL_STARTUP_MAX:
        return False, f"启动仅 {elapsed:.0f}s（< {STALL_STARTUP_MAX}）"
    return True, f"启动 {elapsed:.0f}s 仍停在 {status}、当日 0 成功"


def _maybe_restart_startup(hb: dict, procs: list, suffix: str = "",
                           dry: bool = False) -> bool:
    """H82：startup 超时 ⇒ taskkill + 拉起。

    返回 True 表示"本轮到此为止"（已动作、dry-run 已判定、或额度耗尽需人工），
    调用方据此 ``return 0``，不再往下走判据 B/HANG。
    """
    need, reason = _startup_hang_decision(hb, procs)
    if not need:
        log(f"[判定] startup 超时不触发：{reason}{suffix}")
        return False

    # 收盘后不动作：此时水位已满，手动启动的实例（复盘/演练）本来就不产新 bar，
    # 600s 后照判就成了误杀。与判据 A 的收盘护栏保持同一口径。
    if _today_watermark_reached_close():
        log(f"[判定] startup 超时不动作：今日水位已达收盘（{reason}）{suffix}")
        return False

    if dry:
        log(f"[判定] startup 超时重启被 dry-run 抑制（{reason}）{suffix}")
        return True

    day = str(hb.get("date") or f"{datetime.now():%Y-%m-%d}")
    used = _restart_budget(day)
    if used >= ZOMBIE_MAX_RESTARTS:
        log(f"[ALERT-STARTUP] {reason} → 今日受控重启已用 "
            f"{used}/{ZOMBIE_MAX_RESTARTS}，不再重启{suffix}")
        raise_alert(STARTUP_ALERT,
                    f"H82 startup 卡死但当日重启额度已耗尽（{reason}），需人工介入",
                    level="alert")
        return True

    log(f"[ALERT-STARTUP] {reason} ⇒ 执行受控重启"
        f"（今日第 {used + 1}/{ZOMBIE_MAX_RESTARTS} 次）{suffix}")
    _bump_restart_budget(day, "startup 超时仍无首采")

    if not _kill_live_run(procs):
        log("[ERROR] 部分进程未能终止 ⇒ 放弃本次重启（双实例会互相覆盖同一 CSV）")
        return True
    _sleep(3)
    left = _live_run_processes()
    if left:
        log(f"[ERROR] 仍残留进程 {[p[0] for p in left]} ⇒ 放弃重启（双实例风险）")
        return True

    _restart(f"startup 卡死：{reason}")
    return True


def main() -> int:
    dry = "--dry-run" in sys.argv
    force = "--ignore-window" in sys.argv   # 测试用：跳过时间窗限制
    now = datetime.now()

    if not force and not _in_window(now):
        # 窗口外静默退出，不写日志（避免每天几十条噪声）
        return 0

    suffix = " [DRY-RUN]" if dry else ""

    # ---- 读心跳 ----
    if not HEARTBEAT.exists():
        log(f"[ALERT] heartbeat.json 不存在 → 今日从未启动{suffix}")
        if not dry:
            _restart("心跳文件缺失")
        return 0

    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[ALERT] heartbeat.json 解析失败: {e!r}{suffix}")
        return 0

    hb_date = hb.get("date")
    updated_at = _parse_dt(hb.get("updated_at") or "")
    last_success = hb.get("last_success_at")
    last_success_at = _parse_dt(last_success or "")
    age = (now - updated_at).total_seconds() if updated_at else None
    succ_age = (now - last_success_at).total_seconds() if last_success_at else None

    log(f"[检查] date={hb_date} status={hb.get('status')} "
        f"updated_at={hb.get('updated_at')} 距今={f'{age:.0f}s' if age is not None else 'N/A'} "
        f"last_success={last_success} "
        f"距今={f'{succ_age:.0f}s' if succ_age is not None else 'N/A'} "
        f"collect_err={hb.get('collect_errors')}{suffix}")

    # ---- H81：账本里有 pending（冷启动未验完）⇒ 优先续验，其余判据让位 ----
    # 不加这一段的话，新实例还没写心跳时 age 会一路涨到判据 A/死亡分支，
    # 那里会直接再 `/Run` 一次 —— 等于绕过额度、可能叠出第二个实例。
    _day = str(hb_date or f"{datetime.now():%Y-%m-%d}")
    if isinstance(_load_stall_ledger(_day).get("pending"), dict):
        if dry:
            log(f"[判定] 存在待续验的恢复（pending）→ dry-run 不动作{suffix}")
            return 0
        _maybe_restart_stall(hb, succ_age, suffix)
        return 0

    # ---- 判据 A：进程不存在（最硬的"人没了"信号，优先于心跳年龄）----
    # 2026-11-09 实证：12:36:49 / 12:56:49 两次 exit 0 静默退出，心跳分别只停在
    # 195s / 495s。旧规则只看到"age < STALE_SECONDS(1200)"就判"循环还活着"，只做
    # H36 告警 ⇒ 死透后干等 13 分钟才可能进死亡分支。进程缺失是直接证据，
    # 只要心跳已停够 PROC_GONE_SECONDS（排除计划任务拉起窗口）就立即重启。
    # PROC_QUERY_TRUSTED=False 时 [] 只代表"查不到"，退回原心跳年龄判据。
    procs = _live_run_processes()
    if (not procs and PROC_QUERY_TRUSTED and age is not None
            and age >= PROC_GONE_SECONDS):
        if _today_watermark_reached_close():
            log(f"[OK] 心跳静默 {age:.0f}s 且无 live_run 进程，但今日水位已达收盘 → 不重启{suffix}")
            return 0
        log(f"[ALERT-DEAD] 无 live_run 进程且心跳已停 {age:.0f}s（进程缺失判据）"
            f"→ 不等 {STALE_SECONDS // 60} 分钟静默阈值{suffix}")
        if not dry:
            _restart(f"进程缺失，心跳已停 {age:.0f}s")
        return 0

    # ---- H82：startup 卡死（进程在、当日 0 成功、启动超上限）----
    # 放在判据 A 之后：进程已消失的归 A；这里只处理"进程还在但起不来"。
    # 11-13 实证：这一路原先要等到 HANG 的 1200s 心跳静默阈值，白等 1498s。
    if _maybe_restart_startup(hb, procs, suffix, dry=dry):
        return 0

    # ---- 判据 B：用 updated_at（循环存活）----
    if age is not None and age <= STALE_SECONDS:
        if _today_watermark_reached_close():
            log(f"[OK] 全部启用组合今日水位已收盘，不报采集陈旧{suffix}")
            return 0
        # 循环还活着。但"活着"≠"在采数"—— H36 的盲区正藏在这一句之后：
        # 采集停摆时 updated_at 照刷、age 恒小，旧版会直接判健康退出。
        _report_success_staleness(hb, succ_age, suffix)
        # H60：活着但**从未采到数** ⇒ 受控重启（杀进程 + 拉起），否则会像 10-27 那样
        # 空转到收盘。只处理 succ_age is None，其余走 H79。
        if not dry:
            if not _maybe_restart_zombie(hb, succ_age, suffix):
                # H79：有过成功记录、但成功已长时间停摆且错误在累积 ⇒ 受控重启。
                # 11-10 实证：AD SDK 会话卡死后每轮硬超时 90s、心跳照刷 3.5 小时，
                # 旧逻辑只告警不动作；重启一次即恢复（11.8s 取到数）。
                _maybe_restart_stall(hb, succ_age, suffix)
        return 0   # 健康

    # ---- 心跳静默：分三种情况处置 ----
    log(f"[判定] 心跳静默 {age:.0f}s；检出 live_run 进程 {len(procs)} 个"
        f"{[p[0] for p in procs] if procs else ''}{suffix}")

    if procs:
        # 进程还在但心跳停刷 → 卡死。H62：不再只告警 —— 当日 0 成功的悬挂会 taskkill 后重启。
        log(f"[ALERT-HANG] 心跳静默 {age:.0f}s 但仍有 live_run 进程存活 "
            f"{[p[0] for p in procs]} → 疑为卡死{suffix}")
        _maybe_restart_hang(hb, succ_age, procs, suffix, dry=dry)
        return 0

    if _today_watermark_reached_close():
        log(f"[OK] 心跳静默 {age:.0f}s，但今日水位已达 {MARKET_CLOSE_HHMM} → 正常收盘，不重启{suffix}")
        return 0

    log(f"[ALERT-DEAD] 心跳静默 {age:.0f}s 且无 live_run 进程、今日数据未到收盘 → 判定静默死亡{suffix}")
    if not dry:
        _restart(f"心跳静默 {age:.0f}s 且无进程")
    return 0


def _restart(reason: str, wait: bool = True) -> None:
    """触发计划任务。

    ⚠️ 这里**不判定"重启成功"**：旧版以"心跳 20s 内刷新"为成功判据，而停摆进程
    本来就每轮刷心跳 ⇒ 什么都没做也会打印"✅ 重启生效"（H79 复核发现的假绿）。
    真正的成功判据由调用方按业务口径验（H79：实例身份 + last_success 前进 + 水位推进）。
    """
    log(f"[动作] 触发重启 {TASK_NAME}（原因：{reason}）")
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME],
                           capture_output=True, timeout=60)
        log(f"[动作] schtasks 返回码={r.returncode}")
    except Exception as e:
        log(f"[ERROR] 触发失败: {e!r}")
        return

    if not wait:
        return
    time.sleep(20)
    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        ua = _parse_dt(hb.get("updated_at") or "")
        if ua and (datetime.now() - ua).total_seconds() < 120:
            log(f"[动作] 进程已起（心跳刷新至 {hb.get('updated_at')}）"
                f"⚠️ 仅证明进程在写心跳，**未证明采数恢复**")
        else:
            log(f"[动作] ⚠️ 重启后心跳仍未刷新（updated_at={hb.get('updated_at')}）→ 需人工介入")
    except Exception as e:
        log(f"[动作] ⚠️ 重启后心跳读取失败: {e!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        log(f"[ERROR] 看门狗自身异常: {traceback.format_exc()}")
        raise SystemExit(1)
