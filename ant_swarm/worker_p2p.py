"""Worker P2P — direct Worker-to-Worker communication via handoff + territory release.

Complements the Hub + Pheromone layers with direct Worker messaging for:
  - Task handoff without Commander intervention
  - Direct context transfer between Workers
  - "I'm stuck, can someone help?" → nearest capable Worker auto-responds

This is the missing P2P layer that closes the collaboration gap between
centralized Hub dispatch and fully decentralized ant-pheromone routing.

Usage:
    from ant_swarm.worker_p2p import WorkerP2P
    p2p = WorkerP2P("worker_memory_01", hub_url="http://127.0.0.1:8765")
    p2p.broadcast_help("vol3 can't parse this profile, anyone?")
    p2p.send_to("worker_binary_03", context_data)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .pheromone import (
    PheromoneDB, Pheromone,
    TRAIL_TTL_MS, ALARM_TTL_MS, RECRUIT_TTL_MS, TERRITORY_TTL_MS,
)
from .protocol import WorkerState


# ── Message types ──────────────────────────────────────────────────────

@dataclass
class P2PMessage:
    """A direct message between Workers."""
    msg_id: str
    from_worker: str
    to_worker: str = ""              # empty = broadcast
    msg_type: str = "info"           # info | help_request | handoff | result | alert
    subject: str = ""
    payload: dict = field(default_factory=dict)
    timestamp: str = ""
    ttl_seconds: int = 300           # auto-expire after 5 min

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "from": self.from_worker,
            "to": self.to_worker,
            "type": self.msg_type,
            "subject": self.subject,
            "payload": self.payload,
            "timestamp": self.timestamp or datetime.now().isoformat(),
            "ttl": self.ttl_seconds,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "P2PMessage":
        return cls(
            msg_id=d.get("msg_id", ""),
            from_worker=d.get("from", ""),
            to_worker=d.get("to", ""),
            msg_type=d.get("type", "info"),
            subject=d.get("subject", ""),
            payload=d.get("payload", {}),
            timestamp=d.get("timestamp", ""),
            ttl_seconds=d.get("ttl", 300),
        )


# ── P2P Manager ────────────────────────────────────────────────────────

class WorkerP2P:
    """Direct Worker-to-Worker communication layer.

    Uses the existing PheromoneDB as the message bus (SQLite WAL mode),
    avoiding the need for a separate message broker. Workers write messages
    as pheromones and other Workers poll for messages addressed to them.

    Three communication patterns:
      1. Direct message: send_to(worker_id, ...) — point-to-point
      2. Broadcast help: broadcast_help(...) — anyone can respond
      3. Handoff request: request_handoff(task_id, target_role) — capability-aware
    """

    def __init__(self, worker_id: str, hub_url: str = "http://127.0.0.1:8765",
                 pheromone_db_path: str = "", dialect: Any = None):
        self.worker_id = worker_id
        self.hub_url = hub_url
        self._db_path = pheromone_db_path or ".ant_swarm/pheromone.db"
        self._db: Optional[PheromoneDB] = None
        self._msg_counter = 0
        # Ant-dialect codec — None 时退化为明文 (向后兼容)
        self.dialect = dialect

    @property
    def db(self) -> PheromoneDB:
        if self._db is None:
            self._db = PheromoneDB(self._db_path)
            self._db.connect()
        return self._db

    # ── Sending ────────────────────────────────────────────────────

    def send_to(self, target_worker: str, subject: str,
                payload: Optional[dict] = None, msg_type: str = "info") -> str:
        """Send a direct message to a specific Worker."""
        msg = P2PMessage(
            msg_id=self._next_id(),
            from_worker=self.worker_id,
            to_worker=target_worker,
            msg_type=msg_type,
            subject=subject,
            payload=payload or {},
            timestamp=datetime.now().isoformat(),
        )
        self._persist(msg)
        return msg.msg_id

    def broadcast_help(self, subject: str, task_context: Optional[dict] = None,
                       required_role: str = "") -> str:
        """Broadcast a help request to all Workers (or specific role).

        Other Workers poll for messages addressed to '' (broadcast) and
        can respond if capable.
        """
        payload = task_context or {}
        if required_role:
            payload["required_role"] = required_role

        msg = P2PMessage(
            msg_id=self._next_id(),
            from_worker=self.worker_id,
            to_worker="",              # broadcast
            msg_type="help_request",
            subject=subject,
            payload=payload,
            ttl_seconds=600,           # help requests live longer
        )
        self._persist(msg, pheromone_type="recruit")
        return msg.msg_id

    def request_handoff(self, task_id: str, target_role: str,
                        reason: str = "", context: Optional[dict] = None) -> str:
        """Request task handoff to a Worker of the specified role.

        This triggers capability-aware routing: only Workers matching
        target_role should pick up the handoff.
        """
        payload = context or {}
        payload.update({
            "task_id": task_id,
            "target_role": target_role,
            "reason": reason or f"Handoff requested by {self.worker_id}",
        })

        msg = P2PMessage(
            msg_id=self._next_id(),
            from_worker=self.worker_id,
            to_worker="",
            msg_type="handoff",
            subject=f"Handoff: {task_id} → {target_role}",
            payload=payload,
            ttl_seconds=900,           # handoffs persist 15 min
        )
        self._persist(msg, pheromone_type="trail")
        return msg.msg_id

    def send_result(self, target_worker: str, task_id: str,
                    result: dict) -> str:
        """Send completed results back to the requesting Worker."""
        return self.send_to(
            target_worker,
            subject=f"Result: {task_id}",
            payload={"task_id": task_id, "result": result},
            msg_type="result",
        )

    # ── Receiving ──────────────────────────────────────────────────

    def poll_messages(self, msg_types: Optional[List[str]] = None,
                      max_age_seconds: int = 300) -> List[P2PMessage]:
        """Poll for messages addressed to this Worker (or broadcast).

        Returns unread messages, newest first.
        """
        messages: List[P2PMessage] = []
        now = time.time()

        # Read from pheromone DB — look for trail/recruit types
        for ptype in ("trail", "recruit", "alarm"):
            records = self.db.scan(ptype=ptype)
            for r in records:
                # Parse the JSON value
                data = r.parse_value()
                if not data:
                    continue

                # Decode payload if it was sent with an ant-dialect
                payload = data.get("payload", {})
                if data.get("dialect_id") and self.dialect is not None:
                    try:
                        payload = self.dialect.decode(payload)
                    except Exception as _e:
                        logging.getLogger(__name__).warning("p2p operation failed", exc_info=True)

                msg = P2PMessage(
                    msg_id=data.get("msg_id", str(r.id)),
                    from_worker=data.get("from", r.worker_id),
                    to_worker=data.get("to", ""),
                    msg_type=data.get("type", "info"),
                    subject=data.get("subject", ""),
                    payload=payload,
                    timestamp=data.get("timestamp", ""),
                    ttl_seconds=data.get("ttl", 300),
                )

                # Filter: must be addressed to us OR broadcast
                if msg.to_worker and msg.to_worker != self.worker_id:
                    continue

                # Filter by type
                if msg_types and msg.msg_type not in msg_types:
                    continue

                # Filter by age (max_age_seconds=0 means show nothing)
                age = now - r.created_at
                if max_age_seconds == 0 or age > max_age_seconds:
                    continue

                messages.append(msg)

        messages.sort(key=lambda m: m.timestamp, reverse=True)
        return messages

    def poll_help_requests(self, my_roles: Optional[List[str]] = None,
                           max_age_seconds: int = 300) -> List[P2PMessage]:
        """Poll for help requests that this Worker can handle.

        Filters by role capability: if a help_request specifies a required_role,
        only Workers with matching roles see it.
        """
        all_help = self.poll_messages(
            msg_types=["help_request", "handoff"],
            max_age_seconds=max_age_seconds,
        )

        if not my_roles:
            return all_help

        # Filter by role compatibility
        compatible = []
        for msg in all_help:
            required = msg.payload.get("required_role", "")
            target = msg.payload.get("target_role", "")
            role_needed = required or target
            if not role_needed or role_needed in my_roles:
                compatible.append(msg)

        return compatible

    def accept_help_request(self, msg_id: str) -> bool:
        """Accept a help request — mark as claimed via territory pheromone."""
        try:
            self.db.deposit(
                ptype="territory",
                key=f"p2p_claim:{msg_id}",
                worker_id=self.worker_id,
                value=json.dumps({
                    "msg_id": msg_id,
                    "claimed_by": self.worker_id,
                    "claimed_at": datetime.now().isoformat(),
                }),
                ttl_ms=TERRITORY_TTL_MS,
            )
            return True
        except Exception as _e:
            logging.getLogger(__name__).warning("p2p operation failed", exc_info=True)
            return False

    # ── Internal ───────────────────────────────────────────────────

    def _next_id(self) -> str:
        self._msg_counter += 1
        return f"p2p_{self.worker_id}_{int(time.time())}_{self._msg_counter:04d}"

    def _persist(self, msg: P2PMessage, pheromone_type: str = "trail") -> None:
        """Persist message as a pheromone in the shared DB.

        只加密 payload 字段——信封 (from/to/type/subject/msg_id) 保持明文，
        路由与 score_tasks 仍可读信封。
        """
        try:
            data = msg.to_dict()
            if self.dialect is not None:
                data["payload"] = self.dialect.encode(msg.payload)
                data["dialect_id"] = self.dialect.id
            self.db.deposit(
                ptype=pheromone_type,
                key=f"p2p_msg:{msg.msg_id}",
                worker_id=self.worker_id,
                value=data,
                ttl_ms=msg.ttl_seconds * 1000,
            )
        except Exception as _e:
            logging.getLogger(__name__).warning("p2p operation failed", exc_info=True)
