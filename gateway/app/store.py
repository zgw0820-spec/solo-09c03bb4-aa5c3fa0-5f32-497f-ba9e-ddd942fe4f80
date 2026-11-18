"""Local edge-gateway simulator.

The gateway owns the *at-most-once* side of the contract:

* a command id is executed at most once, ever -- uniqueness is enforced in
  SQLite, so a retried POST can only return the already-recorded outcome;
* the execution and its receipt are committed in one transaction before the
  caller can observe anything, so "executed but receipt lost" is recoverable
  by querying the command id;
* dropping a receipt never drops or rolls back the execution record.

States stored per command id:
    running   accepted, execution not yet committed (outcome uncertain)
    executed  execution committed; receipt_status succeeded/failed
    unknown   gateway restarted while the command was running.  The simulator
              cannot prove whether the device was touched, so this id is
              *never* executed again and is reported as "needs human
              reconciliation".

Boot generations and device reports
-----------------------------------
A *successful* restart execution bumps the device's boot ``generation`` in
the same transaction that commits the execution, and the generation is
stamped onto the execution record (``boot_generation``).  The simulated
device then emits online/offline *reports*; every report is stamped by the
gateway with the device's current generation and a per-generation sequence
number.  Callers never supply either number -- a report's ``(generation,
seq)`` is always gateway-assigned, so a hand-typed generation is not
evidence of anything.

A report can be *held* (emitted but not yet arrived) and released later,
which honestly simulates a late report: it keeps its emission-time
``(generation, seq)`` even when it arrives after a reboot, so an old
generation or an old sequence number can never masquerade as fresh.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS executions (
    command_id     TEXT PRIMARY KEY,
    device_id      TEXT NOT NULL,
    state          TEXT NOT NULL,
    receipt_status TEXT,
    executions     INTEGER NOT NULL DEFAULT 0,
    received_at    TEXT NOT NULL,
    executed_at    TEXT,
    drop_receipt   INTEGER NOT NULL DEFAULT 0,
    boot_generation INTEGER
);

CREATE INDEX IF NOT EXISTS by_device ON executions(device_id);

CREATE TABLE IF NOT EXISTS devices (
    device_id  TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    generation  INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    online      INTEGER NOT NULL,
    arrived     INTEGER NOT NULL DEFAULT 1,
    emitted_at  TEXT NOT NULL,
    arrived_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS report_gen_seq ON reports(device_id, generation, seq);
CREATE INDEX IF NOT EXISTS report_device ON reports(device_id, id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class GatewayStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Migration for databases created before boot generations existed.
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(executions)")
            }
            if "boot_generation" not in cols:
                conn.execute(
                    "ALTER TABLE executions ADD COLUMN boot_generation INTEGER"
                )
        # A command that was mid-flight when the process died has an unknown
        # outcome.  Never turn that into "not executed": the row is frozen as
        # `unknown` so a retried delivery cannot execute it a second time.
        with self._tx() as conn:
            conn.execute(
                "UPDATE executions SET state='unknown' WHERE state='running'"
            )

    def get(self, command_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM executions WHERE command_id=?", (command_id,)
            ).fetchone()

    def list(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM executions ORDER BY received_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def accept(
        self, command_id: str, device_id: str, drop_receipt: bool
    ) -> tuple[str, Optional[sqlite3.Row]]:
        """Reserve the command id.

        Returns ``(action, row)`` where action is:
          duplicate -- id already settled; row is the stored record
          busy      -- reserved and currently running; row is the record
          other     -- id belongs to another device; row is the record
          start     -- caller must perform the one execution

        An `unknown` row is reported as duplicate: the execution may have
        happened, so this id must never be run again.
        """
        now = utcnow()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is not None:
                if row["device_id"] != device_id:
                    return "other", row
                if row["state"] == "running":
                    return "busy", row
                return "duplicate", row
            conn.execute(
                "INSERT INTO executions (command_id, device_id, state, executions, "
                "received_at, drop_receipt) VALUES (?, ?, 'running', 0, ?, ?)",
                (command_id, device_id, now, 1 if drop_receipt else 0),
            )
            return "start", conn.execute(
                "SELECT * FROM executions WHERE command_id=?", (command_id,)
            ).fetchone()

    def commit_execution(
        self,
        command_id: str,
        device_id: str,
        *,
        status: str = "succeeded",
        delay_seconds: float = 0.0,
    ) -> sqlite3.Row:
        """Perform and record the one real execution.

        A delay simulates device work; it happens *before* the transaction so
        no lock is held, but the RUNNING reservation already exists, so a
        concurrent duplicate request cannot execute in parallel.

        A successful restart boots the device into a new generation: the
        device counter is bumped and stamped onto the execution record in the
        same transaction, so "executed" and "new boot generation issued" can
        never disagree.
        """
        if delay_seconds > 0:
            time.sleep(delay_seconds)
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE command_id=?", (command_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("execution reservation disappeared")
            if row["state"] != "running":
                return row
            cur = conn.execute(
                "UPDATE executions SET state='executed', receipt_status=?, "
                "executions=executions+1, executed_at=? "
                "WHERE command_id=? AND state='running' AND executions=0",
                (status, utcnow(), command_id),
            )
            if cur.rowcount != 1:  # defence in depth: never double-execute
                raise RuntimeError("execution already committed")
            if status == "succeeded":
                conn.execute(
                    "INSERT INTO devices (device_id, generation) VALUES (?, 1) "
                    "ON CONFLICT(device_id) DO UPDATE SET generation=generation+1",
                    (device_id,),
                )
                generation = conn.execute(
                    "SELECT generation FROM devices WHERE device_id=?",
                    (device_id,),
                ).fetchone()["generation"]
                conn.execute(
                    "UPDATE executions SET boot_generation=? WHERE command_id=?",
                    (generation, command_id),
                )
            return conn.execute(
                "SELECT * FROM executions WHERE command_id=?", (command_id,)
            ).fetchone()

    def set_drop_receipt(self, command_id: str) -> bool:
        """Make the response to the current / next delivery vanish.

        Called while the gateway is still inside `commit_execution` (the
        simulated device delay) it also affects that in-flight response, since
        the committed row is re-read right before replying.
        """
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE executions SET drop_receipt=1 WHERE command_id=?",
                (command_id,),
            )
            return cur.rowcount == 1

    # ---------------------------------------------------- devices and reports
    def get_device(self, device_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()

    def add_report(
        self, device_id: str, *, online: bool, arrive: bool = True
    ) -> sqlite3.Row:
        """The simulated device emits an online/offline report.

        The gateway -- never the caller -- stamps the report with the
        device's *current* generation and the next sequence number inside
        that generation.  With ``arrive=False`` the report is emitted but
        held "in the network"; it keeps its stamp when released later, which
        is how a late old report is simulated.
        """
        now = utcnow()
        with self._tx() as conn:
            dev = conn.execute(
                "SELECT generation FROM devices WHERE device_id=?", (device_id,)
            ).fetchone()
            generation = dev["generation"] if dev is not None else 0
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM reports "
                "WHERE device_id=? AND generation=?",
                (device_id, generation),
            ).fetchone()["next_seq"]
            cur = conn.execute(
                "INSERT INTO reports (device_id, generation, seq, online, "
                "arrived, emitted_at, arrived_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    device_id,
                    generation,
                    seq,
                    1 if online else 0,
                    1 if arrive else 0,
                    now,
                    now if arrive else None,
                ),
            )
            return conn.execute(
                "SELECT * FROM reports WHERE id=?", (cur.lastrowid,)
            ).fetchone()

    def release_reports(self, device_id: str) -> int:
        """All held reports of the device arrive now, keeping their original
        (generation, seq) stamps -- they may belong to an old boot by now."""
        with self._tx() as conn:
            cur = conn.execute(
                "UPDATE reports SET arrived=1, arrived_at=? "
                "WHERE device_id=? AND arrived=0",
                (utcnow(), device_id),
            )
            return cur.rowcount

    def list_reports(self, device_id: str, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM reports WHERE device_id=? ORDER BY id DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
