"""Transactional per-child bootstrap and current-period reconciliation."""

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.mesh.exceptions import MeshAuthError
from app.mesh.normalized import MeshClient
from app.repository.database import Database

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(self, mesh: MeshClient, db: Database, timezone: ZoneInfo) -> None:
        self.mesh, self.db, self.timezone = mesh, db, timezone
        self.lock = asyncio.Lock()

    def now(self) -> datetime:
        return datetime.now(self.timezone)

    async def run(self, *, homework_only: bool = False) -> bool:
        async with self.lock:
            now = self.now()
            today = now.date()
            try:
                students = await self.mesh.get_profile()
                await self.db.save_students(students)
            except Exception as exc:
                await self.failed(exc, now)
                return False
            success = True
            for index, student in enumerate(students, 1):
                try:
                    if not homework_only:
                        averages = await self.mesh.get_subject_averages(student, today)
                        starts = [a.period_start for a in averages if a.period_start]
                        academic_start = date(
                            today.year if today.month >= 9 else today.year - 1, 9, 1
                        )
                        # For one family a full current-period reconciliation is small
                        # and reliably catches late and edited marks, even weeks later.
                        start = min(starts) if starts else academic_start
                        initial = await self.db.state(f"bootstrap:{student.id}") != "done"
                        marks = await self.mesh.get_marks(student, start, today)
                        count = await self.db.save_marks(
                            student, marks, averages, now.isoformat(), initial
                        )
                        logger.info(
                            "mesh.sync child=%s marks=%s new=%s bootstrap=%s",
                            index,
                            len(marks),
                            count,
                            initial,
                        )
                    end = today + timedelta(days=14)
                    homework = await self.mesh.get_homework(student, today, end)
                    await self.db.save_homework(
                        student, homework, today.isoformat(), end.isoformat(), now.isoformat()
                    )
                    logger.info("mesh.sync child=%s homework=%s", index, len(homework))
                except Exception as exc:
                    await self.failed(exc, now)
                    success = False
            if success:
                await self.db.set_state("auth", "ok", now.isoformat())
                await self.db.set_state("last_error", "", now.isoformat())
                key = "last_homework_sync" if homework_only else "last_sync"
                await self.db.set_state(key, now.isoformat(), now.isoformat())
                if not homework_only:
                    await self.db.set_state("last_homework_sync", now.isoformat(), now.isoformat())
            return success

    async def failed(self, exc: Exception, now: datetime) -> None:
        # Exception bodies can contain provider details. Persist/log only the class.
        logger.warning("mesh.sync failed error=%s", type(exc).__name__)
        await self.db.set_state("last_error", type(exc).__name__, now.isoformat())
        await self.db.set_state(
            "auth", "required" if isinstance(exc, MeshAuthError) else "api_error", now.isoformat()
        )
