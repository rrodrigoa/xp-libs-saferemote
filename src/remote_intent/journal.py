"""Transactional SQLite replay ledger, state and transition evidence."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import canonical
from .errors import IntentError

_ACTIVE = ("accepted", "running")


class Journal:
    """Durable endpoint state. Deploy into a directory with operator-only ACLs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise IntentError("INTERNAL_ERROR")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY, nonce TEXT UNIQUE NOT NULL, digest TEXT NOT NULL,
                    principal TEXT NOT NULL, status TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
                    result BLOB
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, execution_id TEXT NOT NULL,
                    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, step_id TEXT,
                    state TEXT NOT NULL, output BLOB, code TEXT
                );
                CREATE INDEX IF NOT EXISTS execution_events ON events(execution_id, seq);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def accept(
        self, execution_id: str, nonce: str, fingerprint: str, principal: str
    ) -> bool:
        """Atomically claim a delivery. Return false for the identical replay."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id, nonce, digest FROM executions WHERE id=? OR nonce=?",
                (execution_id, nonce),
            ).fetchall()
            if rows:
                if len(rows) == 1 and tuple(rows[0]) == (
                    execution_id,
                    nonce,
                    fingerprint,
                ):
                    return False
                raise IntentError("PLAN_REPLAY_CONFLICT")
            db.execute(
                "INSERT INTO executions(id, nonce, digest, principal, status) VALUES (?, ?, ?, ?, 'accepted')",
                (execution_id, nonce, fingerprint, principal),
            )
            db.execute(
                "INSERT INTO events(execution_id, state) VALUES (?, 'accepted')",
                (execution_id,),
            )
        return True

    def record(
        self,
        execution_id: str,
        state: str,
        *,
        step: str | None = None,
        output: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        encoded = canonical(output) if output is not None else None
        with self.connection() as db:
            if step is None:
                db.execute(
                    "UPDATE executions SET status=?, result=? WHERE id=?",
                    (state, encoded, execution_id),
                )
            db.execute(
                "INSERT INTO events(execution_id, step_id, state, output, code) VALUES (?, ?, ?, ?, ?)",
                (execution_id, step, state, encoded, code),
            )

    def status(self, execution_id: str, principal: str) -> dict[str, Any]:
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute(
                """SELECT status, (
                       SELECT code FROM events WHERE execution_id=executions.id
                       AND step_id IS NULL ORDER BY seq DESC LIMIT 1
                   ) AS code FROM executions WHERE id=? AND principal=?""",
                (execution_id, principal),
            ).fetchone()
            if row is None:
                raise IntentError("SECURITY_DENIED")
            steps = db.execute(
                "SELECT step_id, state, code FROM events WHERE execution_id=? AND step_id IS NOT NULL ORDER BY seq",
                (execution_id,),
            )
            return {
                "execution_id": execution_id,
                "status": row["status"],
                "code": row["code"],
                "steps": {
                    step["step_id"]: {"status": step["state"], "code": step["code"]}
                    for step in steps
                },
            }

    def results(self, execution_id: str, principal: str) -> dict[str, Any]:
        with self.connection() as db:
            row = db.execute(
                "SELECT result FROM executions WHERE id=? AND principal=?",
                (execution_id, principal),
            ).fetchone()
        if row is None:
            raise IntentError("SECURITY_DENIED")
        result: dict[str, Any] = (
            json.loads(row["result"]) if row["result"] is not None else {}
        )
        return result

    def cancel(self, execution_id: str, principal: str) -> bool:
        self.status(execution_id, principal)
        with self.connection() as db:
            return (
                db.execute(
                    "UPDATE executions SET cancelled=1 WHERE id=? AND status IN (?, ?)",
                    (execution_id, *_ACTIVE),
                ).rowcount
                == 1
            )

    def cancelled(self, execution_id: str) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT cancelled FROM executions WHERE id=?", (execution_id,)
            ).fetchone()
        return bool(row["cancelled"])

    def recover_exclusive(self) -> int:
        """Quarantine interrupted runs while all endpoint workers are stopped."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO events(execution_id, state, code)
                       SELECT id, 'needs_reconciliation', 'RECONCILIATION_REQUIRED'
                       FROM executions WHERE status IN (?, ?)""",
                _ACTIVE,
            )
            return db.execute(
                "UPDATE executions SET status='needs_reconciliation' WHERE status IN (?, ?)",
                _ACTIVE,
            ).rowcount
