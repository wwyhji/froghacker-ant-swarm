"""Ant-colony pheromone communication layer for CCM.

Replaces central HTTP Hub dispatch with a SQLite WAL-based shared state space.
Workers scan pheromones and self-select tasks — no central scheduler needed.

Four pheromone types:
  trail     — "I solved this, here's how" (positive feedback, decays in 30min)
  alarm     — "Something is blocking work" (persists until resolved)
  recruit   — "I need help with this" (urgent, deadline-driven)
  territory — "I'm working on this" (CAS claim, TTL 60s, needs renewal)

Design: knowledge/solved/ant_pheromone_plan.md
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── TTL defaults (milliseconds) ──
TRAIL_TTL_MS     = 30 * 60 * 1000   # 30 min
ALARM_TTL_MS     =  6 * 60 * 60 * 1000  # 6 hours
RECRUIT_TTL_MS   = 24 * 60 * 60 * 1000  # 24 hours (overridden by deadline)
TERRITORY_TTL_MS = 60 * 1000        # 60 seconds (must renew)

DEFAULT_DB_PATH = ".ant_swarm/pheromone.db"


@dataclass
class Pheromone:
    """A single pheromone record from the database."""
    id: int
    type: str          # trail | alarm | recruit | territory
    key: str           # e.g. "qid:C01", "blocker:disk_full"
    worker_id: str
    value: str         # JSON payload
    ttl_ms: int
    created_at: float  # time.time()

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_ms / 1000.0

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def parse_value(self) -> dict:
        """Parse the JSON value field."""
        try:
            return json.loads(self.value)
        except (json.JSONDecodeError, TypeError):
            return {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "key": self.key,
            "worker_id": self.worker_id,
            "value": self.parse_value(),
            "ttl_ms": self.ttl_ms,
            "created_at": self.created_at,
            "expires_in_s": max(0, self.expires_at - time.time()),
        }


class PheromoneDB:
    """SQLite WAL-based pheromone store.

    Usage:
        db = PheromoneDB(".ant_swarm/pheromone.db")
        db.connect()

        # Deposit a trail
        db.deposit("trail", "qid:C01", "worker_1",
                   {"confidence": "high", "finding": "Found XSS"})

        # Scan active pheromones
        active = db.scan()

        # CAS claim a territory
        if db.claim("qid:C01", "worker_2"):
            # Got it — work, then deposit trail
            db.deposit("trail", "qid:C01", "worker_2", result)
        db.renew("qid:C01", "worker_2")  # keep-alive
        db.release("qid:C01", "worker_2")  # done
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    # ── lifecycle ──

    def connect(self) -> sqlite3.Connection:
        """Open (or create) the pheromone database with WAL mode."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=OFF")
        self._init_schema(conn)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pheromone (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                type        TEXT NOT NULL,
                key         TEXT NOT NULL,
                worker_id   TEXT NOT NULL,
                value       TEXT NOT NULL DEFAULT '{}',
                ttl_ms      INTEGER NOT NULL,
                created_at  REAL NOT NULL,
                UNIQUE(type, key, worker_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pheromone_active
                ON pheromone(type, key, created_at);
            CREATE INDEX IF NOT EXISTS idx_pheromone_worker
                ON pheromone(worker_id);
        """)
        conn.commit()

    # ── deposit ──

    def deposit(self, ptype: str, key: str, worker_id: str,
                value: dict | None = None, ttl_ms: int | None = None) -> int:
        """Deposit a pheromone. Uses INSERT OR REPLACE (upsert by type+key+worker).

        Returns the row id.
        """
        if value is None:
            value = {}
        if ttl_ms is None:
            ttl_ms = _default_ttl(ptype)

        now = time.time()
        cur = self.conn.execute("""
            INSERT OR REPLACE INTO pheromone (type, key, worker_id, value, ttl_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (ptype, key, worker_id, json.dumps(value, ensure_ascii=False), ttl_ms, now))
        self.conn.commit()
        return cur.lastrowid

    # ── scan ──

    def scan(self, ptype: str | None = None, key: str | None = None,
             worker_id: str | None = None) -> list[Pheromone]:
        """Read active (non-expired) pheromones. Optional filters.

        TTL expiration is checked at read time — no daemon needed.
        """
        now_ms = time.time() * 1000
        where = ["(created_at * 1000 + ttl_ms) > ?"]
        params: list = [now_ms]

        if ptype:
            where.append("type = ?")
            params.append(ptype)
        if key:
            where.append("key = ?")
            params.append(key)
        if worker_id:
            where.append("worker_id = ?")
            params.append(worker_id)

        sql = f"SELECT id, type, key, worker_id, value, ttl_ms, created_at FROM pheromone WHERE {' AND '.join(where)} ORDER BY created_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [Pheromone(*row) for row in rows]

    # ── CAS claim (territory) ──

    def claim(self, key: str, worker_id: str, ttl_ms: int = TERRITORY_TTL_MS) -> bool:
        """Atomically claim a territory. Returns True if successful.

        True CAS: BEGIN IMMEDIATE grabs the SQLite write lock up front, so the
        check-then-insert below is serialized across workers — no two workers can
        pass the check concurrently and double-claim the same key.

        (The previous version used a DEFERRED transaction and relied on the WAL
        snapshot upgrade failing with SQLITE_BUSY_SNAPSHOT to reject a stale
        check — implicit and fragile. IMMEDIATE makes the mutual exclusion
        explicit.)
        """
        now = time.time()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            return False  # another writer holds the lock (busy)
        try:
            # Clean expired territories on this key first
            self.conn.execute("""
                DELETE FROM pheromone
                WHERE type = 'territory'
                  AND key = ?
                  AND (created_at * 1000 + ttl_ms) <= ?
            """, (key, now * 1000))

            # Check if anyone else holds an active territory on this key
            existing = self.conn.execute("""
                SELECT worker_id FROM pheromone
                WHERE type = 'territory'
                  AND key = ?
                  AND (created_at * 1000 + ttl_ms) > ?
            """, (key, now * 1000)).fetchone()

            if existing and existing[0] != worker_id:
                self.conn.rollback()
                return False  # Another worker holds the territory

            # Claim (or renew) territory
            self.conn.execute("""
                INSERT OR REPLACE INTO pheromone (type, key, worker_id, value, ttl_ms, created_at)
                VALUES ('territory', ?, ?, '{}', ?, ?)
            """, (key, worker_id, ttl_ms, now))
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            self.conn.rollback()
            return False

    # ── renew ──

    def renew(self, key: str, worker_id: str) -> bool:
        """Renew territory TTL (keep-alive). Returns True if still owner."""
        now = time.time()
        cur = self.conn.execute("""
            UPDATE pheromone
            SET created_at = ?, ttl_ms = ?
            WHERE type = 'territory' AND key = ? AND worker_id = ?
              AND (created_at * 1000 + ttl_ms) > ?
        """, (now, TERRITORY_TTL_MS, key, worker_id, now * 1000))
        self.conn.commit()
        return cur.rowcount > 0

    # ── release ──

    def release(self, key: str, worker_id: str) -> bool:
        """Release a territory claim."""
        cur = self.conn.execute("""
            DELETE FROM pheromone
            WHERE type = 'territory' AND key = ? AND worker_id = ?
        """, (key, worker_id))
        self.conn.commit()
        return cur.rowcount > 0

    # ── cleanup ──

    def cleanup(self) -> int:
        """Remove ALL expired pheromones. Call periodically (low frequency)."""
        now_ms = time.time() * 1000
        cur = self.conn.execute("""
            DELETE FROM pheromone
            WHERE (created_at * 1000 + ttl_ms) <= ?
        """, (now_ms,))
        self.conn.commit()
        return cur.rowcount

    def vacuum(self) -> None:
        """Reclaim disk space after large cleanups."""
        self.conn.execute("VACUUM")

    # ── stats ──

    def stats(self) -> dict:
        """Return counts by pheromone type."""
        rows = self.conn.execute("""
            SELECT type, COUNT(*) FROM pheromone
            WHERE (created_at * 1000 + ttl_ms) > ?
            GROUP BY type
        """, (time.time() * 1000,)).fetchall()
        return {row[0]: row[1] for row in rows}


# ═══════════════════════════════════════════════════════════════
# Task scoring (pure functions — no DB access)
# ═══════════════════════════════════════════════════════════════

# Outcome strings that mark a trail as a negative ("dead-end") signal.
NEGATIVE_OUTCOMES = frozenset({
    "dead_end", "dead-end", "deadend",
    "failure", "failed", "fail",
    "blocked", "error",
})


def _trail_direction(value) -> int:
    """Classify a trail's feedback direction.

    Returns +1 for a positive trail, -1 for a dead-end trail.

    A trail is negative when its payload records a failed attempt, so a
    "此路不通" marker is not mistaken for "此路通". Trails with no outcome
    marker default to positive (backward compatible with pre-direction data).
    """
    if not isinstance(value, dict):
        return 1
    if not value:
        return 1

    outcome = value.get("outcome")
    if isinstance(outcome, str) and outcome.strip().lower() in NEGATIVE_OUTCOMES:
        return -1

    # Boolean success markers used by other layers (e.g. ant_swarm: {"done": False}).
    for flag in ("done", "success"):
        if value.get(flag) is False:
            return -1

    # Nested result markers: {"result": "failed"} / {"result": {"status": "failed"}}.
    result = value.get("result")
    if isinstance(result, str) and result.strip().lower() in NEGATIVE_OUTCOMES:
        return -1
    if isinstance(result, dict):
        status = result.get("status")
        if isinstance(status, str) and status.strip().lower() in NEGATIVE_OUTCOMES:
            return -1

    return 1


def score_tasks(pheromones: list[Pheromone], worker_id: str, role: str = "") -> dict[str, float]:
    """Score each task key based on pheromone concentrations.

    Rules (pure local decision):
      a) Territory held by another worker → score = -1 (skip)
      b) Each positive trail on key → +1; a dead-end trail → -1
      c) Recruit urgency (closer to deadline = higher score); a recruit with
         a role hint is only open to workers of that role (soft match)
      d) Alarm on key → penalty (blocker unresolved)

    Returns {key: score}. Keys with score < 0 are unavailable.
    """
    scores: dict[str, float] = {}
    trail_scores: dict[str, int] = {}
    blocked_keys: set[str] = set()

    for p in pheromones:
        key = p.key

        if p.type == "territory":
            if p.worker_id != worker_id:
                scores[key] = -1.0  # Occupied by another worker
            # Own territory = neutral

        elif p.type == "trail":
            trail_scores[key] = trail_scores.get(key, 0) + _trail_direction(p.parse_value())

        elif p.type == "alarm":
            blocked_keys.add(key)

        elif p.type == "recruit":
            val = p.parse_value()
            # Soft role matching: a role-hinted recruit is only open to a
            # matching-role worker; a role-less recruit is open to all.
            hint = val.get("role")
            if hint and hint != role:
                scores[key] = -1.0  # wrong skill — don't help (avoid harm)
                continue
            urgency = _deadline_score(val.get("deadline"), val.get("deadline_hours"))
            scores[key] = scores.get(key, 0) + urgency

    # Apply trail density bonus (only to non-blocked, non-occupied keys)
    for key, delta in trail_scores.items():
        if scores.get(key, 0) >= 0 and key not in blocked_keys:
            scores[key] = scores.get(key, 0) + delta

    # Apply blocker penalty
    for key in blocked_keys:
        if scores.get(key, 0) >= 0:
            scores[key] = max(0, scores.get(key, 0) - 5)

    return scores


def pick_best_task(pheromones: list[Pheromone], worker_id: str,
                   role: str = "") -> Optional[str]:
    """Pick the best task key for a worker.

    Args:
        pheromones: Active pheromones from scan()
        worker_id: This worker's ID
        role: Worker's role, for role-aware scoring

    Returns:
        Best task key, or None if no tasks available.
    """
    scores = score_tasks(pheromones, worker_id, role)

    # Filter unavailable
    available = {k: v for k, v in scores.items() if v >= 0}
    if not available:
        return None

    # Sort by score descending, then by key for determinism
    ranked = sorted(available.items(), key=lambda x: (-x[1], x[0]))
    return ranked[0][0]


# ── helpers ──

def _default_ttl(ptype: str) -> int:
    return {
        "trail": TRAIL_TTL_MS,
        "alarm": ALARM_TTL_MS,
        "recruit": RECRUIT_TTL_MS,
        "territory": TERRITORY_TTL_MS,
    }.get(ptype, TRAIL_TTL_MS)


def _deadline_score(deadline: str | None, hours: int | None) -> float:
    """Score urgency: closer to deadline = higher score (max 10)."""
    if hours is not None and hours > 0:
        # Linear decay: 10 at 0h, 0 at 24h+
        return max(0, 10 * (1 - hours / 24))
    if deadline:
        try:
            # ISO format: "2026-06-24T12:00:00"
            from datetime import datetime
            dt = datetime.fromisoformat(deadline)
            remaining_h = max(0, (dt - datetime.now()).total_seconds() / 3600)
            return max(0, 10 * (1 - remaining_h / 24))
        except (ValueError, TypeError):
            pass
    return 1.0  # No deadline → low urgency
