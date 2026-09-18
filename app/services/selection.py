"""Per-chat child selection; every allow-listed user sees the same family."""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from app.mesh.models import Student
from app.repository.database import Database


@dataclass(frozen=True)
class StudentSelection:
    students: tuple[Student, ...]
    selected: tuple[Student, ...]

    @property
    def student_ids(self) -> frozenset[str]:
        return frozenset(student.id for student in self.selected)

    @property
    def key(self) -> str:
        return hashlib.sha256(",".join(sorted(self.student_ids)).encode()).hexdigest()[:16]


class SelectionService:
    def __init__(
        self, db: Database, allowed_chats: frozenset[int], report_chat_id: int | None = None
    ) -> None:
        self.db, self.allowed_chats = db, allowed_chats
        self.configured_channel = report_chat_id

    async def report_channel(self) -> int | None:
        stored = await self.db.state("report_channel")
        channel = int(stored) if stored else self.configured_channel
        return channel if channel is not None and channel < 0 else None

    async def bind_channel(self, chat: int, now: datetime) -> None:
        if chat >= 0:
            raise ValueError("channel ID must be negative")
        await self.db.set_state("report_channel", str(chat), now.isoformat())

    async def scope(self, chat: int) -> StudentSelection | None:
        if chat not in self.allowed_chats and chat != await self.report_channel():
            return None
        students = await self.db.students()
        if not students:
            return None
        chosen = await self.db.state(f"selected_student:{chat}")
        selected = [student for student in students if student.id == chosen]
        return StudentSelection(tuple(students), tuple(selected or students))

    async def select(self, chat: int, student_id: str | None, now: datetime) -> bool:
        scope = await self.scope(chat)
        if not scope or student_id is not None and student_id not in {s.id for s in scope.students}:
            return False
        await self.db.set_state(f"selected_student:{chat}", student_id or "all", now.isoformat())
        return True
