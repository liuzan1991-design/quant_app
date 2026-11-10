# -*- coding: utf-8 -*-
"""H80 验收工具：读 `exit_trace.jsonl`，判每个实例「有始有终 / untraced」。

判据（与 H80 记录一致）
=======================
- `start` 有、`exit` 无  ⇒ **untraced**：进程不是从 Python 层退出的
  （SIGABRT/SIGSEGV 原生崩溃，或 `exit()`/`TerminateProcess` 外部终止）。
  此时去 `console_<date>.log` 找 `Fatal Python error` 区分：
  **有转储 = 原生崩溃；无转储 = 静默终止**。
- `exit_kind = SystemExit(...)` ⇒ 有代码显式退出（SDK `sys.exit(0)` 属此类）。
- `exit_kind = normal_return` ⇒ 收盘/非交易日正常收尾。
- `thread_exception` ⇒ 线程级死亡，进程还活着（H60/H79「进程在但零成功」的线索）。

用法
====
    D:/Anaconda3/python.exe scripts/analyze_exit_trace.py [--path live_outputs/logs/exit_trace.jsonl]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

UNTRACED = "untraced"


def load(path: Path) -> list[dict]:
    recs = []
    if not path.exists():
        return recs
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return recs


def summarize(recs: list[dict]) -> dict:
    """纯函数：记录 → 结构化的验收视图（便于测试，不读写文件）。"""
    starts = {}
    exits = {}
    threads = []
    for r in recs:
        ev = r.get("event")
        pid = str(r.get("pid"))
        if ev == "start":
            starts.setdefault(pid, r)          # 同一 pid 只认首条
        elif ev == "exit":
            exits.setdefault(pid, r)           # 幂等：只认首条
        elif ev == "thread_exception":
            threads.append(r)

    instances = []
    for pid, s in starts.items():
        e = exits.get(pid)
        kind = e.get("exit_kind") if e else UNTRACED
        instances.append({
            "pid": pid,
            "started_at": s.get("started_at"),
            "day": str(s.get("started_at") or "")[:10],
            "exit_kind": kind,
            "uptime_s": (e or {}).get("uptime_s"),
            "last_stage": (e or s).get("stage"),
            "detail": (e or {}).get("detail") or {},
        })
    instances.sort(key=lambda x: str(x["started_at"]))

    by_day = defaultdict(lambda: Counter())
    for i in instances:
        by_day[i["day"]][i["exit_kind"]] += 1

    short = [i for i in instances
             if isinstance(i["uptime_s"], (int, float)) and i["uptime_s"] < 600]

    return {
        "instances": instances,
        "total": len(instances),
        "kinds": Counter(i["exit_kind"] for i in instances),
        "by_day": {k: dict(v) for k, v in sorted(by_day.items())},
        "untraced": [i for i in instances if i["exit_kind"] == UNTRACED],
        "short_lived": short,
        "thread_exceptions": threads,
    }


def render(s: dict) -> str:
    out = []
    out.append(f"实例总数 {s['total']}")
    out.append("退出类型分布：" + ("，".join(f"{k}={v}" for k, v in s["kinds"].items()) or "（无）"))
    out.append("")
    out.append("| 启动时刻 | pid | 退出类型 | 存活(s) | 最后阶段 |")
    out.append("|---|---|---|---|---|")
    for i in s["instances"]:
        out.append(f"| {i['started_at']} | {i['pid']} | {i['exit_kind']} | "
                   f"{i['uptime_s'] if i['uptime_s'] is not None else '—'} | {i['last_stage']} |")

    out.append("")
    if s["untraced"]:
        out.append(f"⚠ untraced {len(s['untraced'])} 个 ⇒ 非 Python 层退出，"
                   f"请对照 console_<date>.log 的 `Fatal Python error`："
                   f"有转储=原生崩溃，无转储=静默终止")
        for i in s["untraced"]:
            out.append(f"  - pid={i['pid']} 起于 {i['started_at']} 最后阶段={i['last_stage']}")
    else:
        out.append("✅ 无 untraced：每个实例都有 exit 记录")

    if s["short_lived"]:
        out.append(f"\n短命实例（<10 分钟）{len(s['short_lived'])} 个：")
        for i in s["short_lived"]:
            out.append(f"  - {i['started_at']} pid={i['pid']} {i['exit_kind']} "
                       f"{i['uptime_s']}s stage={i['last_stage']}")

    if s["thread_exceptions"]:
        c = Counter((t.get("thread"), t.get("exception")) for t in s["thread_exceptions"])
        out.append("\n线程级异常（进程未死）：")
        for (th, ex), n in c.most_common():
            out.append(f"  - {th} / {ex} × {n}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="live_outputs/logs/exit_trace.jsonl")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    s = summarize(load(Path(args.path)))
    if args.json:
        print(json.dumps({k: (v if k != "instances" else v) for k, v in s.items()},
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
