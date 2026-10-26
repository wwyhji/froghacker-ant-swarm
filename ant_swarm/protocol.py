"""Protocol types and state machine for Claude Collaboration."""

from __future__ import annotations

import enum
import threading
import time


class WorkerState(enum.Enum):
    """Worker lifecycle states."""
    IDLE = "idle"
    JOINED = "joined"       # 已注册到 Hub
    CLAIMED = "claimed"     # 已认领任务
    WORKING = "working"     # 执行中
    BLOCKED = "blocked"     # 遇到卡点
    REPORTING = "reporting" # 提交结果
    COMPLETED = "completed" # 完成
    FAILED = "failed"       # 失败
    DISCONNECTED = "disconnected"  # 失联


class MessageType(enum.Enum):
    """Message types exchanged via Hub."""
    HEARTBEAT = "heartbeat"
    TASK_CLAIM = "task_claim"
    TASK_COMPLETE = "task_complete"
    TASK_RELEASE = "task_release"
    FINDING = "finding"
    BLOCKER = "blocker"
    ANSWER = "answer"
    VERIFY_REQUEST = "verify_request"
    VERIFY_RESPONSE = "verify_response"
    CONTEXT_HANDOFF = "context_handoff"
    HELP_REQUEST = "help_request"


_ORDERED_STATES = [
    WorkerState.IDLE,
    WorkerState.JOINED,
    WorkerState.CLAIMED,
    WorkerState.WORKING,
    WorkerState.BLOCKED,
    WorkerState.REPORTING,
    WorkerState.COMPLETED,
]

VALID_TRANSITIONS = {
    WorkerState.IDLE:         {WorkerState.JOINED, WorkerState.DISCONNECTED},
    WorkerState.JOINED:       {WorkerState.CLAIMED, WorkerState.IDLE, WorkerState.DISCONNECTED},
    WorkerState.CLAIMED:      {WorkerState.WORKING, WorkerState.FAILED, WorkerState.IDLE},
    WorkerState.WORKING:      {WorkerState.BLOCKED, WorkerState.REPORTING, WorkerState.FAILED},
    WorkerState.BLOCKED:      {WorkerState.WORKING, WorkerState.FAILED, WorkerState.IDLE},
    WorkerState.REPORTING:    {WorkerState.COMPLETED, WorkerState.FAILED},
    WorkerState.COMPLETED:    {WorkerState.IDLE, WorkerState.DISCONNECTED},
    WorkerState.FAILED:       {WorkerState.IDLE, WorkerState.DISCONNECTED},
    WorkerState.DISCONNECTED: {WorkerState.IDLE},
}


class StateMachine:
    """Thread-safe state machine for worker lifecycle."""

    def __init__(self, initial: WorkerState = WorkerState.IDLE):
        self._state = initial
        self._lock = threading.Lock()
        self._changed_at = time.time()

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def changed_at(self) -> float:
        return self._changed_at

    def transition(self, target: WorkerState) -> None:
        """Transition to target state. Raises CCMError if invalid."""
        with self._lock:
            now = self._state
            if target not in VALID_TRANSITIONS.get(now, set()):
                raise CCMError(
                    f"Invalid state transition: {now.value} -> {target.value}"
                )
            self._state = target
            self._changed_at = time.time()

    def can_transition(self, target: WorkerState) -> bool:
        return target in VALID_TRANSITIONS.get(self._state, set())

    def reset(self) -> None:
        with self._lock:
            self._state = WorkerState.IDLE
            self._changed_at = time.time()


class CCMError(Exception):
    """Base error for CCM operations."""


