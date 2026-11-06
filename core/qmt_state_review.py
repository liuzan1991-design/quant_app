"""沙箱外的只读告警及人工已阅台账。所有输出目录必须显式指定。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from qmt_builtin.state_rebuild import rebuild_state


CST = timezone(timedelta(hours=8))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed.astimezone(CST)


def _now():
    return datetime.now(CST).isoformat()


def _write_atomic(path, content):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateReviewStore:
    """SQLite 保存事实和复核，JSON 和文本为可重新生成的只读视图。"""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "state_review.sqlite3"
        with self._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    as_of REAL NOT NULL, observed_at TEXT NOT NULL, state_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS current_states (
                    scope TEXT PRIMARY KEY, observation_id TEXT NOT NULL REFERENCES observations(id)
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    mode TEXT NOT NULL, blockers_json TEXT NOT NULL,
                    resolved_by TEXT REFERENCES observations(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS unique_active_incident
                    ON incidents(scope, fingerprint) WHERE resolved_by IS NULL;
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES incidents(id),
                    reviewer TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.database, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record(self, inputs, *, observed_at=None):
        """从原始标准化证据重新计算，已阅记录不参与重建。"""
        state = rebuild_state(**inputs)
        as_of = _time(state["as_of"]).timestamp()
        timestamp = _time(observed_at).isoformat() if observed_at else _now()
        scope = state["symbol"] + ":" + state["strategy_id"]
        observation_id = _digest({"inputs": inputs, "state": state})
        fingerprint = _digest({"mode": state["mode"], "blockers": sorted(set(state["blockers"]))})
        with self._transaction() as connection:
            connection.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?, ?)",
                               (observation_id, scope, as_of, timestamp, _json(state)))
            previous = connection.execute("""
                SELECT o.* FROM observations o JOIN current_states c ON c.observation_id=o.id
                WHERE c.scope=?
            """, (scope,)).fetchone()
            # 旧数据只存审计。相同时点的矛盾证据不能解除停手。
            update_current = previous is None or as_of > previous["as_of"]
            if previous is not None and as_of == previous["as_of"]:
                old_state = json.loads(previous["state_json"])
                severity = {"ready": 0, "reduce_only": 1, "halt": 2}
                update_current = previous["id"] == observation_id or (
                    state["mode"] != "ready" and
                    severity[state["mode"]] >= severity[old_state["mode"]])
            if update_current:
                connection.execute("INSERT OR REPLACE INTO current_states VALUES (?, ?)",
                                   (scope, observation_id))
                if state["mode"] == "ready" and not state["blockers"]:
                    connection.execute("""
                        UPDATE incidents SET resolved_by=? WHERE scope=? AND resolved_by IS NULL
                    """, (observation_id, scope))
                else:
                    incident = connection.execute("""
                        SELECT id FROM incidents WHERE scope=? AND fingerprint=? AND resolved_by IS NULL
                    """, (scope, fingerprint)).fetchone()
                    if incident:
                        connection.execute("UPDATE incidents SET last_seen=MAX(last_seen, ?) WHERE id=?",
                                           (timestamp, incident["id"]))
                    else:
                        incident_id = _digest({"scope": scope, "observation_id": observation_id})
                        connection.execute("INSERT INTO incidents VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                                           (incident_id, scope, fingerprint, timestamp, timestamp,
                                            state["mode"], _json(state["blockers"])))
        self.publish()
        return state

    def acknowledge(self, incident_id, reviewer, note, *, request_id=None):
        """仅追加人工已阅，不关闭告警，不修改任何重建输入或权限。"""
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("必须填写复核人")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("必须填写复核意见")
        review_id = _digest({"incident": incident_id, "request": request_id}) if request_id else _digest(
            {"incident": incident_id, "reviewer": reviewer, "note": note})
        with self._transaction() as connection:
            incident = connection.execute("SELECT id FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if incident is None:
                raise ValueError("复核事件不存在")
            old = connection.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            if old and (old["incident_id"], old["reviewer"], old["note"]) != (incident_id, reviewer, note):
                raise ValueError("重复复核请求的内容冲突")
            connection.execute("INSERT OR IGNORE INTO reviews VALUES (?, ?, ?, ?, ?)",
                               (review_id, incident_id, reviewer, note, _now()))
        self.publish()
        return review_id

    @staticmethod
    def _report(connection):
        observations = [dict(row) for row in connection.execute("SELECT * FROM observations ORDER BY rowid")]
        current = {row["scope"]: row["observation_id"] for row in connection.execute("SELECT * FROM current_states")}
        for row in observations:
            row["state"] = json.loads(row.pop("state_json"))
        reviews = [dict(row) for row in connection.execute("SELECT * FROM reviews ORDER BY rowid")]
        reviewed = {row["incident_id"] for row in reviews}
        incidents = [dict(row) for row in connection.execute("SELECT * FROM incidents ORDER BY rowid")]
        for row in incidents:
            row["blockers"] = json.loads(row.pop("blockers_json"))
            row["active"] = row["resolved_by"] is None
            row["review_status"] = "acknowledged_waiting_evidence" if row["id"] in reviewed else "pending"
            if not row["active"]:
                row["review_status"] = "resolved_by_rebuild"
        return {"current_observations": current, "observations": observations,
                "incidents": incidents, "reviews": reviews}

    def report(self):
        with self._transaction() as connection:
            return self._report(connection)

    def publish(self):
        # 重新取得写锁后生成视图，避免并发导出把新状态覆盖成旧状态。
        with self._transaction() as connection:
            report = self._report(connection)
            active = [row for row in report["incidents"] if row["active"]]
            lines = ["QMT 只读停手告警", "人工已阅仅作登记，交易权限由原始证据重新计算。", ""]
            if not active:
                lines.append("当前没有待证据恢复的告警。")
            for incident in active:
                lines.extend(["事件 " + incident["id"], "范围 " + incident["scope"],
                              "状态 " + incident["mode"] + " / " + incident["review_status"],
                              "阻断原因 " + ", ".join(incident["blockers"]), ""])
            _write_atomic(self.root / "review_report.json", _json(report) + "\n")
            _write_atomic(self.root / "ALERT_ACTIVE.txt", "\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="显式指定独立台账目录")
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="从标准化证据重建并登记告警")
    record.add_argument("--input", type=Path, required=True)
    acknowledge = commands.add_parser("ack", help="记录人工已阅，保留门禁")
    acknowledge.add_argument("--incident", required=True)
    acknowledge.add_argument("--reviewer", required=True)
    acknowledge.add_argument("--note", required=True)
    acknowledge.add_argument("--request-id")
    commands.add_parser("show", help="查看当前告警和复核记录")
    args = parser.parse_args(argv)
    try:
        store = StateReviewStore(args.out)
        if args.command == "record":
            inputs = json.loads(args.input.read_text(encoding="utf-8-sig"))
            state = store.record(inputs)
            report = store.report()
            scope = state["symbol"] + ":" + state["strategy_id"]
            current_id = report["current_observations"][scope]
            current = next(row["state"] for row in report["observations"] if row["id"] == current_id)
            print(_json({"mode": current["mode"], "blockers": current["blockers"],
                         "input_mode": state["mode"],
                         "report": str(store.root / "review_report.json")}))
            return 2 if current["mode"] == "halt" or current["blockers"] else 0
        if args.command == "ack":
            review_id = store.acknowledge(args.incident, args.reviewer, args.note, request_id=args.request_id)
            print(_json({"review_id": review_id, "effect": "acknowledgement_only"}))
        else:
            print(_json(store.report()))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error) as error:
        print(_json({"status": "review_io_or_input_error", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
