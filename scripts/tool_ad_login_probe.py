# -*- coding: utf-8 -*-
"""AD（星耀数智）登录可用性探针 —— 只读诊断工具，不写任何生产数据。

用途：**每日开盘前**判断"今天到底能不能采到数"。2026-10-14（D21）全天空跑后新增：
AD 登录在**服务端/账号层**被拒时，`live_run` 只会报一句自造的 `TimeoutError("疑似网络不通")`
（机理见纠偏记录 **H34 家族 · 诊断可用性补充**），本工具把**真话**取出来。

判据分层（与 `tool_qmt_ad_reconcile.py` 的 preflight 同一纪律）：
  · **硬判据 = 能否完成 `ad.login()`**（TCP 可达只是**诊断线索**：可达 ≠ 能登录）
  · 登录成功后再验一层：**登录成功 ≠ 数据可读** ⇒ 试读指定标的当日 1m 行数
退出码：0 = 登录成功且数据可读；4 = 登录成功但数据读不到；3 = 登录失败；2 = 前置不可达（凭证/ TCP）

🔒 防泄漏设计（**必须保持，勿简化**）
    星耀 SDK 会把**会话 Token 与权限列表**写出来（fd 1/2 + Python 层 print 两条路都可能有）。
    因此本脚本**不采用** `core/ad_silence.suppress_sdk_output()`（它会把报错话术一并吞掉），
    而是把登录**放进子进程**、由父进程在创建时就把该子进程的 stdout/stderr **指向临时文件**
    （子进程的 fd 1/2 在诞生那一刻就是文件 ⇒ 即使 SDK 绕过 Python 层的重定向也漏不出去）；
    事后**只提取含失败关键词的行**与**本脚本的标记行**，**绝不整段回显**，用完即删。
    ⚠️ 教训：2026-10-14 首版只做了 `os.dup2`（漏了 Python 层 `sys.stdout` 缓冲）⇒ **确实漏了一次 Token**。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
CRED_DEFAULT = Path(r"D:\Codex输出\ad_credentials.json")
LOGIN_TIMEOUT = 60
SDK_KEY = re.compile(r"(?i)fail|error|denied|invalid|expire|refus|timeout|cannot|unable")
MARK = "__ADPROBE__"


def tcp_probe(host, port, timeout=8):
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, (time.time() - t0) * 1000.0
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:90])


# --------------------------------------------------------------------------- worker
def run_worker(cred_path, timeout, code, day) -> int:
    """子进程内执行：登录 + 数据可读性验证。fd 1/2 已由父进程指向临时文件。"""
    cred = json.loads(Path(cred_path).read_text(encoding="utf-8"))
    out = {"ok": False, "exc": None, "elapsed": None, "alive": False}
    box = {}

    def worker():
        import AmazingData as ad
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(30)
        try:
            ad.login(username=cred["username"], password=cred["password"],
                     host=cred["host"], port=int(cred["port"]))
            box["ad"] = ad
        except BaseException as exc:  # 必须抓 BaseException：SDK 用 sys.exit(0) 收尾
            box["exc"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        finally:
            socket.setdefaulttimeout(old)

    t0 = time.time()
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    th.join(timeout=timeout)
    out["elapsed"] = time.time() - t0
    out["alive"] = th.is_alive()
    if "exc" in box:
        out["exc"] = box["exc"]
    elif "ad" not in box:
        out["exc"] = "登录阻塞 >%ds（线程未返回）" % timeout
    else:
        out["ok"] = True
    print("%s%s" % (MARK, json.dumps(out, ensure_ascii=False)))

    if out["ok"]:
        data = {"ok": False, "rows": 0, "err": None}
        try:
            ad = box["ad"]
            cal = ad.BaseData().get_calendar()
            md = ad.MarketData(cal)
            pv = getattr(ad.constant.Period, "min1").value
            b = e = int(day.replace("-", ""))
            k = md.query_kline([code], begin_date=b, end_date=e, period=pv)
            n = len(k.get(code, [])) if k and code in k else 0
            data["ok"] = n > 0
            data["rows"] = n
        except Exception as exc:  # noqa: BLE001
            data["err"] = "%s: %s" % (type(exc).__name__, str(exc)[:140])
        print("%sDATA%s" % (MARK, json.dumps(data, ensure_ascii=False)))
    sys.stdout.flush()
    return 0


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description="AD 登录可用性探针（只读）")
    ap.add_argument("--cred", default=str(CRED_DEFAULT), help="凭证文件路径")
    ap.add_argument("--timeout", type=int, default=LOGIN_TIMEOUT, help="登录软超时（秒）")
    ap.add_argument("--code", default="300308.SZ", help="数据可读性验证标的")
    ap.add_argument("--date", default=None, help="验证日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args, _ = ap.parse_known_args()

    today = args.date or time.strftime("%Y-%m-%d")

    if args.worker:                                    # 子进程分支
        return run_worker(args.cred, args.timeout, args.code, today)

    print("=== AD 登录探针（只读）===")
    print("  时间        : %s" % time.strftime("%Y-%m-%d %H:%M:%S %z"))
    print("  凭证文件    : %s" % args.cred)
    print("  验证标的/日 : %s @ %s" % (args.code, today))

    cred_path = Path(args.cred)
    if not cred_path.exists():
        print("  [2] ❌ 凭证文件不存在 ⇒ 前置不可达")
        return 2
    cred = json.loads(cred_path.read_text(encoding="utf-8"))
    host, port = cred.get("host"), int(cred.get("port"))
    print("  凭证 host   : %s:%s（mtime %s）" % (
        host, port, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cred_path.stat().st_mtime))))

    ok_tcp, info = tcp_probe(host, port)
    if not ok_tcp:
        print("  [①] TCP 同层探针: ❌ 不可达（%s）" % info)
        print("  [2] 前置不可达 ⇒ 退出（先查网络/代理，再查账号）")
        return 2
    print("  [①] TCP 同层探针: ✅ 可达（%.0f ms）—— 注：可达 ≠ 能登录" % info)

    # ---- 子进程隔离执行（防 Token 泄漏的关键）----
    fd, logpath = tempfile.mkstemp(prefix="ad_probe_", suffix=".log")
    os.close(fd)
    rec = {"ok": None, "exc": None, "elapsed": None, "alive": False}
    data = None
    try:
        with open(logpath, "wb") as sink:
            subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                            "--worker", "--cred", args.cred, "--timeout", str(args.timeout),
                            "--code", args.code, "--date", today],
                           stdout=sink, stderr=sink, timeout=args.timeout + 90)
        raw = Path(logpath).read_text(encoding="utf-8", errors="replace")
        key_lines = []
        for ln in raw.split("\n"):
            ln = ln.strip()
            if ln.startswith(MARK):
                payload = ln[len(MARK):]
                if payload.startswith("DATA"):
                    data = json.loads(payload[4:])
                else:
                    rec.update(json.loads(payload))
            elif ln and SDK_KEY.search(ln):
                key_lines.append(ln[:160])
        key_lines = key_lines[:6]
    finally:
        try:
            os.remove(logpath)
        except OSError:
            pass

    print("  [②] 登录        : %s（耗时 %s s%s）" % (
        "✅ 成功" if rec["ok"] else "❌ 失败",
        ("%.1f" % rec["elapsed"]) if rec["elapsed"] is not None else "?", "，⚠️ 线程未返回" if rec["alive"] else ""))
    if rec["exc"]:
        print("      异常类型    : %s" % rec["exc"])
        if "SystemExit" in rec["exc"]:
            print("      ⚠️ SDK 打印自己的失败话术后用 `sys.exit(0)` 收尾 —— 真因见下方 SDK 行，**不是**超时/网络问题")
    print("      SDK 关键行（已过滤，仅失败类；不含 Token）：%s" % ("" if key_lines else "（无匹配行）"))
    for ln in key_lines:
        print("        | %s" % ln)

    if not rec["ok"]:
        print("  [3] ❌ 登录失败 ⇒ **今日无法采集**（登录层不通，补跑同样登不上）")
        print("      建议：① 联系星耀数智确认账号状态/并发占用/订阅有效期；② 本机侧已由 [①] 排除")
        return 3

    print("  [③] 数据可读性  : 试读 %s %s 1m（登录成功 ≠ 数据可读）" % (args.code, today))
    if data and data["ok"]:
        print("      ✅ 行数 = %d ⇒ 采集条件具备" % data["rows"])
        return 0
    print("      ❌ 读不到数据（rows=%s%s）" % (
        data["rows"] if data else "?", ("，%s" % data["err"]) if data and data["err"] else ""))
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
