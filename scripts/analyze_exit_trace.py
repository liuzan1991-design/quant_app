# -*- coding: utf-8 -*-
"""H80只读分析。缺exit只说明未取得记录；存活和死因须另取证。
run_id配对；旧版仅用(pid, started_at)完整二元组，不猜配。
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

UNTRACED = "untraced"


class TraceRecords(list):
    def __init__(self):
        super().__init__()
        self.issues = []


def _reject_constant(value):
    raise ValueError("non-finite JSON constant")


def load(path: Path) -> TraceRecords:
    """坏行不丢失其异常证据；继续读后续完整记录，但全局禁止绿色结论。"""
    recs = TraceRecords()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        recs.issues.append({"code": "log_missing" if isinstance(exc, FileNotFoundError)
                           else "log_unreadable", "error": type(exc).__name__})
        return recs
    for n, line in enumerate(raw.splitlines(keepends=True), 1):
        if not line.strip():
            continue
        if not line.endswith(b"\n"):
            recs.issues.append({"code": "unterminated_line", "line": n})
        try:
            row = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeError):
            recs.issues.append({"code": "invalid_json_line", "line": n})
            continue
        if not isinstance(row, dict):
            recs.issues.append({"code": "non_object_line", "line": n})
            continue
        recs.append(row)
    return recs


def _identity(r: dict) -> str | None:
    rid = r.get("run_id")
    if isinstance(rid, str) and rid.strip():
        return rid
    if rid is not None:
        return None
    if r.get("schema_version") == 2:
        return None  # 新版损坏记录不能降格冒充旧日志
    pid, started = r.get("pid"), r.get("started_at")
    if pid is not None and isinstance(started, str) and started:
        return f"legacy:{pid}:{started}"
    return None


def summarize(recs: list[dict], liveness: dict | None = None) -> dict:
    """纯函数。liveness由外部只读探针传入running/stopped/unknown；缺省未知。"""
    issues = list(getattr(recs, "issues", []))
    runs = defaultdict(list)
    seen = {}
    threads = []
    for n, r in enumerate(recs, 1):
        if not isinstance(r, dict):
            issues.append({"code": "invalid_record", "record": n})
            continue
        rid = _identity(r)
        ev = r.get("event")
        if rid is None or ev not in {"start", "stage", "exit", "signal", "thread_exception"}:
            issues.append({"code": "invalid_identity_or_event", "record": n})
            continue
        if (isinstance(r.get("pid"), bool) or not str(r.get("pid", "")).isdigit()
                or not isinstance(r.get("started_at"), str) or not r["started_at"]):
            issues.append({"code": "invalid_instance_fields", "record": n})
            continue
        eid = r.get("event_id")
        if eid is not None:
            if not isinstance(eid, str) or not eid:
                issues.append({"code": "invalid_event_id", "record": n})
                continue
            if eid in seen:
                if seen[eid] != r:
                    issues.append({"code": "conflicting_event_id", "record": n})
                continue
            seen[eid] = r
        runs[rid].append(r)
        if ev == "thread_exception":
            threads.append(r)

    instances = []
    for rid, events in runs.items():
        starts = [r for r in events if r["event"] == "start"]
        if not starts:
            issues.append({"code": "orphan_events", "run_id": rid})
            continue
        start = starts[0]
        if events[0]["event"] != "start":
            issues.append({"code": "events_before_start", "run_id": rid})
        identity = (str(start["pid"]), start["started_at"], start.get("process_create_time"))
        if any((str(r["pid"]), r["started_at"], r.get("process_create_time")) != identity
               for r in events):
            issues.append({"code": "conflicting_instance_identity", "run_id": rid})
        if len(starts) > 1:
            issues.append({"code": "duplicate_start", "run_id": rid})
        exits = [r for r in events if r["event"] == "exit"]
        if len(exits) > 1:
            issues.append({"code": "duplicate_exit", "run_id": rid})
        end = exits[0] if exits else None
        if end and (not isinstance(end.get("exit_kind"), str) or not end["exit_kind"]):
            issues.append({"code": "exit_kind_missing", "run_id": rid})
        alive = (liveness or {}).get(rid, "unknown")
        if alive not in {"running", "stopped", "unknown"}:
            alive = "unknown"
        if end:
            status = "finishing" if alive == "running" else "exit_recorded"
            kind = end.get("exit_kind")
        elif alive == "stopped":
            status, kind = "missing_exit", UNTRACED
        else:
            status, kind = ("running" if alive == "running" else "unknown"), None
        latest = events[-1]
        instances.append({
            "run_id": rid, "legacy": start.get("run_id") is None,
            "pid": str(start["pid"]), "process_create_time": start.get("process_create_time"),
            "started_at": start["started_at"], "day": start["started_at"][:10],
            "status": status, "liveness": alive, "exit_kind": kind,
            "uptime_s": end.get("uptime_s") if end else None,
            "observed_uptime_s": latest.get("uptime_s"),
            "last_stage": latest.get("stage"), "last_observed_at": latest.get("at"),
            "detail": (end or {}).get("detail") or {},
            "cause": "undetermined",
        })
    instances.sort(key=lambda x: (x["started_at"], x["run_id"]))
    by_day = defaultdict(Counter)
    for i in instances:
        by_day[i["day"]][i["exit_kind"] or i["status"]] += 1
    missing = [i for i in instances if i["status"] == "missing_exit"]
    short = [i for i in instances if i["status"] in {"exit_recorded", "finishing"}
             and isinstance(i["uptime_s"], (int, float)) and 0 <= i["uptime_s"] < 600]
    return {
        "instances": instances, "total": len(instances),
        "kinds": Counter(i["exit_kind"] or i["status"] for i in instances),
        "by_day": {k: dict(v) for k, v in sorted(by_day.items())},
        "running": [i for i in instances if i["status"] in {"running", "finishing"}],
        "unknown": [i for i in instances if i["status"] == "unknown"],
        "missing_exit": missing, "untraced": missing,
        "short_lived": short, "thread_exceptions": threads,
        "log_issues": issues, "log_integrity": "invalid" if issues else "ok",
    }


def probe_liveness(instances: list[dict], process_factory=None) -> dict:
    """外部只读探针：以PID和精确出生时间核对实例，权限不足/缺标识均未知。"""
    result = {}
    try:
        import psutil
    except ImportError:
        return {i["run_id"]: "unknown" for i in instances}
    factory = process_factory or psutil.Process
    for i in instances:
        status = "unknown"
        try:
            proc = factory(int(i["pid"]))
            recorded = i.get("process_create_time")
            current = proc.create_time()
            if (isinstance(recorded, (int, float)) and not isinstance(recorded, bool)
                    and math.isfinite(recorded) and recorded > 0):
                if current != recorded:
                    status = "stopped"  # PID已被其他实例复用
                elif proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
                    status = "running"
                else:
                    status = "stopped"
        except psutil.NoSuchProcess:
            status = "stopped"
        except Exception:
            pass  # 无权限或探针失败不能当作进程已消失
        result[i["run_id"]] = status
    return result


def render(s: dict) -> str:
    labels = {"running": "运行中", "finishing": "已有退出记录，进程仍在收尾",
              "missing_exit": "已确认实例结束，缺退出记录",
              "unknown": "存活未知，退出记录待核", "exit_recorded": "已取得退出记录"}
    out = [f"实例总数 {s['total']}",
           "| 启动时刻 | run_id | pid | 状态 | 退出记录 | 最后持久化阶段 |",
           "|---|---|---|---|---|---|"]
    for i in s["instances"]:
        out.append(f"| {i['started_at']} | {i['run_id']} | {i['pid']} | "
                   f"{labels[i['status']]} | {i['exit_kind'] or '—'} | {i['last_stage']} |")
    if s["log_issues"]:
        out.append(f"⚠ 日志证据不完整/异常：{len(s['log_issues'])} 项；不得据此给出健康结论。")
        for issue in s["log_issues"]:
            out.append("  - " + json.dumps(issue, ensure_ascii=False))
    if not s["total"]:
        out.append("⚠ 无可配对实例记录；还没观测到完整启动证据，不等于健康。")
    elif s["missing_exit"]:
        out.append(f"⚠ 缺退出记录 {len(s['missing_exit'])} 个（untraced），死因未知。")
    elif s["unknown"]:
        out.append("⚠ 存活未知；不能把缺exit判成已退出或原生终止。")
    elif s["running"]:
        out.append("运行/收尾中的实例暂不要求exit；启动后不应预期normal_return。")
    elif not s["log_issues"]:
        out.append("退出记录配对完整；不代表采集健康或死因已经确认。")
    out.append("死因边界：无转储不能排除原生崩溃；有转储须按实例和时间核对。"
               "SystemExit只说明观察到该异常，不指认SDK；normal_return须结合via/reason判读。")
    if s["short_lived"]:
        out.append(f"短存活退出记录（<10分钟）{len(s['short_lived'])} 个；不触发重启或熔断。")
    if s["thread_exceptions"]:
        out.append(f"线程异常记录 {len(s['thread_exceptions'])} 条；不证明进程当时或现在健康。")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="live_outputs/logs/exit_trace.jsonl")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-process-check", action="store_true", help="仅分析日志，存活一律未知")
    args = ap.parse_args()
    records = load(Path(args.path))
    first = summarize(records)
    lives = {} if args.no_process_check else probe_liveness(first["instances"])
    summary = summarize(records, lives)
    print(json.dumps(summary, ensure_ascii=False, indent=2) if args.json else render(summary))
    return 2 if summary["log_issues"] or summary["unknown"] or not summary["total"] else (
        1 if summary["missing_exit"] else 0)


if __name__ == "__main__":
    raise SystemExit(main())
