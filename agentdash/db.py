"""SQLite 永続化 (標準ライブラリ sqlite3)。保存先は ~/.agentdash/agentdash.db。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  repo_path TEXT NOT NULL,
  worktree_path TEXT,
  branch TEXT,
  base_commit TEXT,
  adapter TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '{}',
  agent_session_id TEXT,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id),
  prompt TEXT NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  exit_code INTEGER,
  cost REAL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id INTEGER NOT NULL REFERENCES turns(id),
  ts REAL NOT NULL,
  type TEXT NOT NULL,
  content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id);
"""

STATUSES = ("running", "waiting", "done", "error", "stopped")


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._con = sqlite3.connect(self.path, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        with self._lock:
            self._con.executescript(SCHEMA)
            self._con.execute("PRAGMA journal_mode=WAL")

    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._con.execute(sql, args)
            self._con.commit()
            return cur

    def _all(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, args).fetchall()]

    # ---- sessions ------------------------------------------------------------
    def create_session(self, name, repo_path, adapter, options, worktree_path=None, branch=None,
                       base_commit=None, status="running") -> int:
        now = time.time()
        cur = self._exec(
            "INSERT INTO sessions(name, repo_path, worktree_path, branch, base_commit, adapter, options, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, repo_path, worktree_path, branch, base_commit, adapter,
             json.dumps(options, ensure_ascii=False), status, now, now))
        return cur.lastrowid

    def update_session(self, sid: int, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        if "options" in fields and not isinstance(fields["options"], str):
            fields["options"] = json.dumps(fields["options"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))

    def get_session(self, sid: int) -> dict | None:
        rows = self._all(SESSION_SELECT + " WHERE s.id=?", (sid,))
        return _session(rows[0]) if rows else None

    def list_sessions(self) -> list:
        return [_session(r) for r in self._all(SESSION_SELECT + " ORDER BY s.updated_at DESC")]

    def delete_session(self, sid: int):
        with self._lock:
            self._con.execute("DELETE FROM events WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?)", (sid,))
            self._con.execute("DELETE FROM turns WHERE session_id=?", (sid,))
            self._con.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self._con.commit()

    def mark_interrupted(self):
        """前回終了時に実行中だったセッションを停止扱いにする (プロセスはもう無い)。"""
        now = time.time()
        self._exec("UPDATE turns SET ended_at=?, exit_code=-1 WHERE ended_at IS NULL", (now,))
        self._exec("UPDATE sessions SET status='stopped', updated_at=? WHERE status='running'", (now,))

    # ---- turns / events --------------------------------------------------------
    def create_turn(self, session_id: int, prompt: str) -> int:
        return self._exec("INSERT INTO turns(session_id, prompt, started_at) VALUES (?,?,?)",
                          (session_id, prompt, time.time())).lastrowid

    def end_turn(self, turn_id: int, exit_code: int | None, cost: float | None):
        self._exec("UPDATE turns SET ended_at=?, exit_code=?, cost=COALESCE(?, cost) WHERE id=?",
                   (time.time(), exit_code, cost, turn_id))

    def add_event(self, turn_id: int, type_: str, content: dict) -> dict:
        ts = time.time()
        cur = self._exec("INSERT INTO events(turn_id, ts, type, content) VALUES (?,?,?,?)",
                         (turn_id, ts, type_, json.dumps(content, ensure_ascii=False)))
        return {"id": cur.lastrowid, "turn_id": turn_id, "ts": ts, "type": type_, "content": content}

    def turns_with_events(self, session_id: int) -> list:
        turns = self._all("SELECT * FROM turns WHERE session_id=? ORDER BY id", (session_id,))
        if not turns:
            return []
        ids = [t["id"] for t in turns]
        q = ",".join("?" * len(ids))
        evs = self._all(f"SELECT * FROM events WHERE turn_id IN ({q}) ORDER BY id", ids)
        by = {i: [] for i in ids}
        for e in evs:
            e["content"] = json.loads(e["content"])
            by[e["turn_id"]].append(e)
        for t in turns:
            t["events"] = by[t["id"]]
        return turns


SESSION_SELECT = """
SELECT s.*,
  (SELECT COUNT(*) FROM turns t WHERE t.session_id=s.id) AS turn_count,
  (SELECT COALESCE(SUM(cost), 0) FROM turns t WHERE t.session_id=s.id) AS total_cost,
  (SELECT e.content FROM events e JOIN turns t ON e.turn_id=t.id
     WHERE t.session_id=s.id AND e.type IN ('text','result','multi') ORDER BY e.id DESC LIMIT 1) AS last_event,
  (SELECT t.prompt FROM turns t WHERE t.session_id=s.id ORDER BY t.id DESC LIMIT 1) AS last_prompt
FROM sessions s
"""


def _session(r: dict) -> dict:
    r["options"] = json.loads(r.get("options") or "{}")
    le = r.pop("last_event", None)
    r["last_message"] = ""
    if le:
        c = json.loads(le)
        if "parts" in c:
            c = next((p for p in reversed(c["parts"]) if p.get("type") == "text"), {})
        r["last_message"] = (c.get("text") or "")[:300]
    return r
