"""Context handoff — pack/unpack Claude execution context for cross-session transfer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class ContextPacket:
    """Serializable context for handoff between Claude instances.

    One Claude packs this when handing off; another unpacks and resumes.
    """
    version: str = "1.0"
    worker_id: str = ""
    task_id: str = ""
    task_description: str = ""
    status: str = "working"

    completed_steps: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    evidence_paths: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    key_observations: str = ""
    open_questions: str = ""
    notes: str = ""

    created_at: str = ""

    def pack(self, **overrides) -> str:
        """Serialize to JSON string."""
        data = asdict(self)
        data.update(overrides)
        if not data.get("created_at"):
            data["created_at"] = datetime.now().isoformat(timespec="seconds")
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def unpack(cls, data: str) -> "ContextPacket":
        """Deserialize from JSON string."""
        raw = json.loads(data)
        return cls(**{k: raw.get(k) for k in cls.__dataclass_fields__})

    def to_summary(self) -> str:
        """Generate human-readable summary for incoming Claude."""
        lines = [
            f"## 上下文交接: {self.task_description}",
            f"来源: {self.worker_id} | 状态: {self.status}",
            f"",
        ]
        if self.completed_steps:
            lines.append(f"### 已完成 ({len(self.completed_steps)} 步)")
            for s in self.completed_steps:
                lines.append(f"  [v] {s}")
            lines.append("")
        if self.findings:
            lines.append(f"### 已发现 ({len(self.findings)} 条)")
            for f in self.findings:
                lines.append(f"  [{f.get('id','?')}] {f.get('summary','')}")
            lines.append("")
        if self.next_steps:
            lines.append(f"### 待完成 ({len(self.next_steps)} 步)")
            for i, s in enumerate(self.next_steps, 1):
                lines.append(f"  {i}. {s}")
            lines.append("")
        if self.blockers:
            lines.append(f"### 卡点 ({len(self.blockers)} 条)")
            for b in self.blockers:
                lines.append(f"  [!] {b.get('reason','')}")
            lines.append("")
        if self.key_observations:
            lines.append(f"### 关键观察")
            lines.append(f"  {self.key_observations}")
            lines.append("")
        if self.open_questions:
            lines.append(f"### 未解决问题")
            lines.append(f"  {self.open_questions}")
            lines.append("")
        if self.notes:
            lines.append(f"### 备注")
            lines.append(f"  {self.notes}")
        return "\n".join(lines)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.pack())

    @classmethod
    def load(cls, path: str) -> "ContextPacket":
        with open(path, "r", encoding="utf-8") as f:
            return cls.unpack(f.read())
