"""Context safety net — event journal, auto-snapshot, local fallback.

Three-tier guarantee:
  Tier 1 — Online: events via Hub, auto-snapshotted every N writes
  Tier 2 — Offline: read last local snapshot from disk
  Tier 3 — Stale: warn user about context age, show what's known
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .protocol import CCMError

EVENT_TYPES = frozenset({
    "worker_joined", "worker_left", "heartbeat",
    "task_pushed", "task_claimed", "task_completed", "task_released",
    "finding_posted", "finding_verified",
    "blocker_created", "blocker_resolved",
    "answer_posted", "answer_verified",
    "context_handoff",
    "strategy_updated", "session_logged",
    "hub_offline", "hub_online",
    "snapshot_taken",
})

SNAPSHOT_INTERVAL = 10  # auto-snapshot every N events
MAX_JOURNAL_BYTES = 1_000_000  # rotate when events.jsonl exceeds 1MB
MAX_ARCHIVE_FILES = 5  # keep at most 5 rotated archives


class EventJournal:
    """Append-only event journal with periodic snapshots.

    Directory layout:
      <cache_dir>/
        events.jsonl        # append-only event stream
        snapshot.json       # latest full context snapshot (overwritten)
        last_seq.txt        # last consumed event sequence number
    """

    def __init__(self, cache_dir: str = ".ant_swarm", worker_id: str = ""):
        self.cache_dir = Path(cache_dir)
        self.worker_id = worker_id
        self._lock = threading.Lock()
        self._seq = 0
        self._event_count_since_snapshot = 0

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._journal_path = self.cache_dir / "events.jsonl"
        self._snapshot_path = self.cache_dir / "snapshot.json"
        self._seq_path = self.cache_dir / "last_seq.txt"
        self._bootstrap_seq_path = self.cache_dir / "bootstrap_seq.txt"
        self._load_seq()

    # ── init helpers ──

    def _load_seq(self):
        try:
            self._seq = int(self._seq_path.read_text().strip())
        except (FileNotFoundError, ValueError):
            self._seq = 0

    def _save_seq(self):
        self._seq_path.write_text(str(self._seq))

    def rotate_if_needed(self, max_bytes: int = MAX_JOURNAL_BYTES) -> bool:
        """Archive events.jsonl if it exceeds max_bytes. Returns True if rotated."""
        if not self._journal_path.exists():
            return False
        size = self._journal_path.stat().st_size
        if size < max_bytes:
            return False
        import gzip
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_name = f"events.{ts}.jsonl.gz"
        archive_path = self.cache_dir / archive_name
        with open(self._journal_path, "rb") as src:
            with gzip.open(archive_path, "wb") as dst:
                import shutil as _shutil
                _shutil.copyfileobj(src, dst)
        # Truncate journal
        self._journal_path.write_text("", encoding="utf-8")
        self._seq = 0
        self._save_seq()
        # Enforce max archives
        archives = sorted(
            self.cache_dir.glob("events.*.jsonl.gz"),
            key=lambda p: p.stat().st_mtime
        )
        for old in archives[:-MAX_ARCHIVE_FILES]:
            old.unlink()
        return True

    # ── write ──

    def write_event(self, event_type: str, data: dict) -> int:
        """Log one event, return its sequence number."""
        if event_type not in EVENT_TYPES:
            event_type = "custom"
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "type": event_type,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "data": data,
            }
            with open(self._journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._event_count_since_snapshot += 1
            self._save_seq()
            return self._seq

    # ── read — replay ──

    def read_events(self, since_seq: int = 0) -> list[dict]:
        """Read all events after `since_seq`. Returns [] if journal missing."""
        if not self._journal_path.exists():
            return []
        events = []
        with open(self._journal_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("seq", 0) > since_seq:
                    events.append(ev)
        return events

    def last_seq(self) -> int:
        return self._seq

    # ── snapshot ──

    def save_snapshot(self, context: dict) -> None:
        """Save a full-context snapshot to disk."""
        snapshot = {
            "seq": self._seq,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "event_count_since_last": self._event_count_since_snapshot,
            "context": context,
        }
        with open(self._snapshot_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        self._event_count_since_snapshot = 0
        self.write_event("snapshot_taken", {"seq": self._seq})

    def should_snapshot(self) -> bool:
        return self._event_count_since_snapshot >= SNAPSHOT_INTERVAL

    def load_snapshot(self) -> Optional[dict]:
        """Load latest snapshot. Returns None if not available."""
        if not self._snapshot_path.exists():
            return None
        try:
            return json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # ── bootstrap seq (persistent dedup across CLI invocations) ──

    def get_bootstrap_seq(self) -> int:
        """Last event seq we showed the user. Persisted to disk."""
        try:
            return int(self._bootstrap_seq_path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return 0

    def set_bootstrap_seq(self, seq: int) -> None:
        """Persist bootstrap seq so next CLI invocation skips seen events."""
        self._bootstrap_seq_path.write_text(str(seq))

    # ── context injection ──

    def build_context_block(self, hub_online: bool,
                            live_progress: Optional[dict] = None,
                            live_findings: Optional[list] = None,
                            live_queue: Optional[list] = None) -> str:
        """Build a human-readable context block for automatic injection.

        Tier 1 — online: use live data from Hub (passed as args)
        Tier 2 — offline: use local snapshot
        Tier 3 — stale: show warning, show whatever we have
        """
        lines = []
        freshness = ""

        if hub_online and live_progress is not None:
            # Tier 1: online — show live data
            freshness = "实时"
            lines.append(f"━━━ 协作环境 ({freshness}) ━━━")
            self._add_workers(lines, live_progress)
            self._add_findings(lines, live_findings)
            self._add_queue(lines, live_queue)
            # auto-snapshot if threshold reached
        else:
            # Tier 2/3: offline — try snapshot
            snap = self.load_snapshot()
            if snap:
                snap_ts = snap.get("timestamp", "unknown")
                age = self._age_seconds(snap_ts)
                if age < 60:
                    freshness = f"快照({int(age)}秒前)"
                elif age < 3600:
                    freshness = f"快照({int(age/60)}分钟前)"
                else:
                    freshness = f"[!] 快照({int(age/3600)}小时前，可能过期)"
                lines.append(f"━━━ 协作环境 ({freshness}) ━━━")
                ctx = snap.get("context", {})
                self._add_workers(lines, ctx.get("progress"))
                self._add_findings(lines, ctx.get("findings"))
                self._add_queue(lines, ctx.get("queue"))
                if age > 300:
                    lines.append("  [!] Hub 离线，快照可能不是最新状态")
            else:
                lines.append("━━━ 协作环境 (无上下文) ━━━")
                lines.append("  [!] Hub 离线且无本地快照，无法获取协作上下文")

        # Add my own info
        if self.worker_id:
            lines.append(f"  我的 Worker ID: {self.worker_id}")
            lines.append(f"  最后事件序号: {self._seq}")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━")
        return "\n".join(lines)

    # ── helpers ──

    @staticmethod
    def _add_workers(lines, progress):
        if progress:
            lines.append(f"  Worker ({len(progress)}):")
            for name, info in sorted(progress.items()):
                s = info.get("status", "?")
                t = info.get("current_task", "")
                blip = "[*]" if s == "working" else "[?]" if s == "blocked" else "[-]"
                lines.append(f"    {blip} {name:20s} [{s:10s}] {t}")
        else:
            lines.append("  Worker: 暂无")

    @staticmethod
    def _add_findings(lines, findings):
        if findings:
            lines.append(f"  发现 ({len(findings)}):")
            for f in (findings or [])[-5:]:  # last 5 only
                lines.append(f"    [{f.get('id','?')}] {f.get('summary','')[:60]}")
        else:
            lines.append("  发现: 暂无")

    @staticmethod
    def _add_queue(lines, queue):
        if queue:
            pending = [t for t in (queue or []) if t.get("status") == "pending"]
            if pending:
                lines.append(f"  待办 ({len(pending)}):")
                for t in pending[:3]:
                    lines.append(f"    {t.get('description','')[:50]}")
        else:
            lines.append("  待办: 暂无")

    @staticmethod
    def _age_seconds(ts: str) -> float:
        try:
            dt = datetime.fromisoformat(ts)
            return (datetime.now() - dt).total_seconds()
        except (ValueError, TypeError):
            return 999999

    # ── event replay summary ──

    def summarize_new_events(self, since_seq: int = 0) -> str:
        """Summarize what changed since last check."""
        events = self.read_events(since_seq)
        if not events:
            return ""
        lines = [f"上次检查后新增 {len(events)} 个事件:"]
        for ev in events:
            t = ev.get("type", "?")
            d = ev.get("data", {})
            ts = ev.get("timestamp", "")
            if t == "finding_posted":
                lines.append(f"  [*] [{ts}] {d.get('summary','')[:50]}")
            elif t == "task_pushed":
                lines.append(f"  [+] [{ts}] 新任务: {d.get('description','')[:40]}")
            elif t == "blocker_created":
                lines.append(f"  [!] [{ts}] 卡点: {d.get('reason','')[:40]}")
            elif t == "blocker_resolved":
                lines.append(f"  [OK] [{ts}] 卡点已解")
            elif t == "worker_joined":
                lines.append(f"  [+] [{ts}] Worker 加入: {d.get('worker_id','')}")
            elif t == "task_completed":
                lines.append(f"  [OK] [{ts}] 任务完成: {d.get('summary','')[:40]}")
            elif t == "snapshot_taken":
                lines.append(f"  [S] [{ts}] 自动快照")
            else:
                lines.append(f"  - [{ts}] {t}")
        return "\n".join(lines)
