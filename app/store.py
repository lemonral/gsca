"""SQLite state and append-only run events for the single-process agent service."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


ACTIVE_STATUSES = ("queued", "running")
STATUSES = {*ACTIVE_STATUSES, "completed", "failed", "cancelled", "interrupted"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Store:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.data_dir / "agent.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, error TEXT,
                    steps INTEGER NOT NULL DEFAULT 0,
                    usage TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_session
                ON runs(session_id) WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS runs_by_session ON runs(session_id, created_at);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL, content TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(id), created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, id);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _one(db, table: str, identity: str) -> dict:
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        out = dict(row)
        if table == "runs":
            out["usage"] = json.loads(out["usage"])
        return out

    def create_session(self, title: str = "新对话") -> dict:
        if not isinstance(title, str):
            raise ValueError("title必须为字符串")
        now, identity = _now(), uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       (identity, title.strip()[:160] or "新对话", now, now))
            return self._one(db, "sessions", identity)

    def rename_session(self, identity: str, title: str) -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title不能为空")
        with self._connect() as db:
            self._one(db, "sessions", identity)
            db.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                       (title.strip()[:160], _now(), identity))
            return self._one(db, "sessions", identity)

    def list_sessions(self) -> list[dict]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC LIMIT 500")]

    def get_session(self, identity: str) -> dict:
        with self._connect() as db:
            return self._one(db, "sessions", identity)

    def messages(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            self._one(db, "sessions", session_id)
            return [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))]

    def create_run(self, session_id: str, prompt: str) -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt不能为空")
        now, identity = _now(), uuid4().hex
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._one(db, "sessions", session_id)
                db.execute("INSERT INTO runs VALUES (?, ?, 'queued', ?, ?, NULL, 0, '{}')",
                           (identity, session_id, now, now))
                db.execute("INSERT INTO messages(session_id, role, content, run_id, created_at) "
                           "VALUES (?, 'user', ?, ?, ?)", (session_id, prompt, identity, now))
                db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
                return self._one(db, "runs", identity)
        except sqlite3.IntegrityError as exc:
            raise ValueError("该会话已有运行中的任务，请等待或取消后再提交") from exc

    def get_run(self, identity: str) -> dict:
        with self._connect() as db:
            return self._one(db, "runs", identity)

    def list_runs(self, session_id: str) -> list[dict]:
        with self._connect() as db:
            self._one(db, "sessions", session_id)
            rows = db.execute("SELECT * FROM runs WHERE session_id=? ORDER BY created_at DESC, id DESC",
                              (session_id,)).fetchall()
            return [{**dict(row), "usage": json.loads(row["usage"])} for row in rows]

    def set_run(self, identity: str, status=None, error=None, steps=None, usage=None) -> dict:
        if status is not None and status not in STATUSES:
            raise ValueError("未知运行状态")
        if steps is not None and (type(steps) is not int or steps < 0):
            raise ValueError("steps必须为非负整数")
        if usage is not None and not isinstance(usage, dict):
            raise ValueError("usage必须为对象")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._one(db, "runs", identity)
            # Terminal runs cannot be resurrected by a late background callback.
            if current["status"] not in ACTIVE_STATUSES and status is not None and status != current["status"]:
                return current
            values = {"updated_at": _now()}
            for key, value in (("status", status), ("error", error), ("steps", steps), ("usage", usage)):
                if value is not None:
                    values[key] = json.dumps(value, ensure_ascii=False) if key == "usage" else value
            clause = ", ".join(f"{key}=?" for key in values)
            try:
                db.execute(f"UPDATE runs SET {clause} WHERE id=?", (*values.values(), identity))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该会话已有活动任务") from exc
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?",
                       (values["updated_at"], current["session_id"]))
            return self._one(db, "runs", identity)

    def add_message(self, session_id: str, role: str, content: str, run_id=None) -> dict:
        if role not in {"user", "assistant", "system", "tool"} or not isinstance(content, str):
            raise ValueError("消息角色或内容无效")
        now = _now()
        with self._connect() as db:
            self._one(db, "sessions", session_id)
            if run_id is not None:
                run = self._one(db, "runs", run_id)
                if run["session_id"] != session_id:
                    raise ValueError("运行与会话不匹配")
            cursor = db.execute("INSERT INTO messages(session_id, role, content, run_id, created_at) "
                                "VALUES (?, ?, ?, ?, ?)", (session_id, role, content, run_id, now))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            return dict(db.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone())

    def event(self, run_id: str, kind: str, payload: dict) -> dict:
        if not isinstance(kind, str) or not kind or not isinstance(payload, dict):
            raise ValueError("事件类型或载荷无效")
        with self._connect() as db:
            self._one(db, "runs", run_id)
            cursor = db.execute("INSERT INTO events(run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                                (run_id, kind, json.dumps(payload, ensure_ascii=False), _now()))
            row = dict(db.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone())
            row["payload"] = json.loads(row["payload"])
            return row

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        if type(after) is not int or after < 0:
            raise ValueError("after必须为非负整数")
        with self._connect() as db:
            self._one(db, "runs", run_id)
            rows = db.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id",
                              (run_id, after)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def recover(self) -> int:
        """Call once on service startup, before accepting requests."""
        now = _now()
        explanation = "服务重启，上一轮任务已中断；请检查已有产物后重新提交。"
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM runs WHERE status IN ('queued', 'running')").fetchall()
            db.execute("UPDATE runs SET status='interrupted', error=?, updated_at=? "
                       "WHERE status IN ('queued', 'running')", (explanation, now))
            for row in rows:
                db.execute("INSERT INTO events(run_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                           (row["id"], "interrupted", json.dumps({"message": explanation}, ensure_ascii=False), now))
            return len(rows)
