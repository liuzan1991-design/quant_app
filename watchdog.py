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

from core.trading_calendar import beijing_today, is_trading_day
from core.alerts import clear_alert, raise_alert   # H36 告警送达
from datetime import datetime, timedelta
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# 测试用：允许用环境变量指向沙箱目录，避免测试污染真实状态
LIVE_DIR = Path(os.environ.get("QUANT_LIVE_DIR") or (APP_DIR / "live_outputs"))
HEARTBEAT = LIVE_DIR / "heartbeat.json"
WATCHDOG_LOG = LIVE_DIR / "logs" / "watchdog.log"
TASK_NAME = "QuantLiveRun"

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
               f" → 仅告警，不重启")
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
                            rss_mb: float | None, water_closed: bool = False
                            ) -> tuple[bool, str]:
    """H79 纯函数：有过成功记录、但成功已长时间停摆 ⇒ 是否受控重启。

    只在「有成功记录」时走这里；「从未成功」归 H60 僵尸路径，二者互斥。
    关键防线见常量区注释：RSS 分界拦住 10-19 型内存泄漏，错误增量免疫午休。
    """
    if succ_age is None:
        return False, "从未成功（归 H60 僵尸路径，不走本判据）"
    if succ_age < SUCCESS_STALL_SECONDS:
        return False, f"上次成功 {succ_age / 60:.0f} 分钟前（< {SUCCESS_STALL_SECONDS // 60}）"
    if err_delta < STALL_MIN_ERRORS:
        return False, f"近轮新增失败仅 {err_delta} 次（< {STALL_MIN_ERRORS}）⇒ 非采集失败，不动"
    if water_closed:
        return False, "今日水位已达收盘，属正常结束"
    if rss_mb is not None and rss_mb >= STALL_RSS_LIMIT_MB:
        return False, (f"RSS {rss_mb:.0f}MB ≥ {STALL_RSS_LIMIT_MB}MB ⇒ 10-19 内存泄漏型，"
                       f"换进程无效（H60 反例）")
    return True, (f"{succ_age / 60:.0f} 分钟无成功、近轮新增失败 {err_delta} 次、"
                  f"RSS {rss_mb:.0f}MB" if rss_mb is not None
                  else f"{succ_age / 60:.0f} 分钟无成功、近轮新增失败 {err_delta} 次（RSS 未知）")


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
    """H79：成功停摆 + 错误累积 ⇒ 杀进程 + 拉起。与 H60 共享每日重启额度。"""
    day = str(hb.get("date") or f"{datetime.now():%Y-%m-%d}")
    current = int(hb.get("collect_errors") or 0)
    delta, prev = _error_delta(day, current)
    _save_collect_errors(day, current)

    water_closed = _today_watermark_reached_close()
    procs = _live_run_processes()
    rss = _live_run_rss_mb(procs)
    need, reason = _success_stall_decision(hb, succ_age, delta, rss, water_closed)
    if not need:
        log(f"[判定] 成功停摆不重启：{reason}{suffix}")
        return False

    used = _restart_budget(day)
    if used >= ZOMBIE_MAX_RESTARTS:
        log(f"[ALERT-STALL] {reason} → 今日受控重启已用 {used}/{ZOMBIE_MAX_RESTARTS}，"
            f"不再重启{suffix}")
        return False

    log(f"[ALERT-STALL] {reason} → 上次成功 {hb.get('last_success_at')}，"
        f"累计失败 {current} 次（上轮 {prev}）⇒ 受控重启"
        f"（今日第 {used + 1}/{ZOMBIE_MAX_RESTARTS} 次）{suffix}")
    _bump_restart_budget(day, f"成功停摆：{reason}")

    if procs:
        if not _kill_live_run(procs):
            log("[ERROR] 部分进程未能终止 ⇒ 放弃本次重启（双实例会互相覆盖同一 CSV）")
            return False
        _sleep(3)
        left = _live_run_processes()
        if left:
            log(f"[ERROR] 仍残留进程 {[p[0] for p in left]} ⇒ 放弃重启（双实例风险）")
            return False

    _restart(f"成功停摆：{reason}")
    return True


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


def _restart(reason: str) -> None:
    log(f"[动作] 触发重启 {TASK_NAME}（原因：{reason}）")
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME],
                           capture_output=True, timeout=60)
        log(f"[动作] schtasks 返回码={r.returncode}")
    except Exception as e:
        log(f"[ERROR] 触发失败: {e!r}")
        return

    # 验证：进程启动后立刻会写 status=starting 的心跳
    import time
    time.sleep(20)
    try:
        hb = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        ua = _parse_dt(hb.get("updated_at") or "")
        if ua and (datetime.now() - ua).total_seconds() < 120:
            log(f"[动作] ✅ 重启生效，心跳已刷新至 {hb.get('updated_at')} status={hb.get('status')}")
        else:
            log(f"[动作] ⚠️ 重启后心跳仍未刷新（updated_at={hb.get('updated_at')}）→ 需人工介入")
    except Exception as e:
        log(f"[动作] ⚠️ 重启验证失败: {e!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        log(f"[ERROR] 看门狗自身异常: {traceback.format_exc()}")
        raise SystemExit(1)
