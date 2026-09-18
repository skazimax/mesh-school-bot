"""Swappable production API: normalized data, serialized refresh and one 401 replay."""

import asyncio
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.mesh.client import ResearchClient, object_payload
from app.mesh.exceptions import MeshAPIError, MeshAuthError
from app.mesh.models import Homework, Mark, Student, SubjectAverage
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).replace(",", "."))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def iso_day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise MeshAPIError("API изменился: некорректная дата в ответе.") from None


class MeshClient(Protocol):
    async def get_profile(self) -> list[Student]: ...
    async def get_marks(self, student: Student, start: date, end: date) -> list[Mark]: ...
    async def get_subject_averages(self, student: Student, today: date) -> list[SubjectAverage]: ...
    async def get_homework(self, student: Student, start: date, end: date) -> list[Homework]: ...
    async def get_schedule(
        self, student: Student, start: date, end: date
    ) -> list[dict[str, Any]]: ...


class MobileMeshClient:
    def __init__(
        self, transport: Transport, store: AuthStore, profile_id: str | None = None
    ) -> None:
        self.raw = ResearchClient(transport, store, profile_id=profile_id)
        self.store = store
        self.lock = asyncio.Lock()
        self.generation = 0

    async def _refresh(self, observed: int) -> None:
        if self.generation != observed:
            return
        state = await self.raw.auth.refresh()
        if not state.mesh_access_token:
            raise MeshAuthError("Обновление не вернуло токен МЭШ.")
        self.raw.token = state.mesh_access_token.get_secret_value()
        self.generation += 1

    async def ensure_auth(self) -> None:
        async with self.lock:
            state = self.store.load()
            from time import time

            if state.mesh_expires_at is not None and state.mesh_expires_at <= time() + 300:
                await self._refresh(self.generation)

    async def get_profile(self) -> list[Student]:
        await self.ensure_auth()
        async with self.lock:
            try:
                profile = await self.raw.get_profile()
            except MeshAuthError:
                await self._refresh(self.generation)
                profile = await self.raw.get_profile()
            return [
                Student(
                    id=str(child["id"]),
                    profile_id=str(self.raw.profile_id),
                    person_id=child.get("contingent_guid") or child.get("person_id"),
                    name=" ".join(
                        str(child.get(key) or "") for key in ("first_name", "last_name")
                    ).strip()
                    or "Ребёнок",
                )
                for child in profile["family"]["children"]
            ]

    async def _fetch(self, student: Student, resource: str, start: date, end: date) -> Any:
        await self.ensure_auth()
        async with self.lock:
            self.raw.profile_id, self.raw.student_id = student.profile_id, student.id
            try:
                return await self.raw.fetch(resource, start, end, person_id=student.person_id)
            except MeshAuthError:
                await self._refresh(self.generation)
                return await self.raw.fetch(resource, start, end, person_id=student.person_id)

    async def get_marks(self, student: Student, start: date, end: date) -> list[Mark]:
        rows = object_payload(await self._fetch(student, "marks", start, end))
        result = []
        try:
            for row in rows:
                day = iso_day(row["date"])
                if not start <= day <= end:
                    raise MeshAPIError("МЭШ проигнорировал фильтр дат оценок.")
                numeric = number(row["value"])
                result.append(
                    Mark(
                        id=str(row["id"]),
                        student_id=student.id,
                        subject_id=str(row["subject_id"]),
                        subject_name=str(row["subject_name"]),
                        value=str(row["value"]),
                        numeric_value=numeric
                        if numeric is not None and 1 <= numeric <= 5
                        else None,
                        weight=int(row.get("weight") or 1),
                        work_type=str(row.get("control_form_name") or ""),
                        comment=str(row.get("comment") or ""),
                        lesson_date=day,
                        created_at_mesh=row.get("created_at"),
                    )
                )
        except (KeyError, ValueError, TypeError):
            raise MeshAPIError("API изменился: некорректная структура оценок.") from None
        return result

    async def get_homework(self, student: Student, start: date, end: date) -> list[Homework]:
        rows = object_payload(await self._fetch(student, "homework", start, end))
        result = []
        try:
            for row in rows:
                day = iso_day(row["date"])
                if not start <= day <= end:
                    raise MeshAPIError("МЭШ проигнорировал фильтр дат ДЗ.")
                result.append(
                    Homework(
                        id=str(row["homework_entry_student_id"]),
                        student_id=student.id,
                        subject_id=str(row["subject_id"]),
                        subject_name=str(row["subject_name"]),
                        lesson_date=day,
                        text=str(row.get("description") or ""),
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise MeshAPIError("API изменился: некорректная структура ДЗ.") from None
        return result

    async def get_subject_averages(self, student: Student, today: date) -> list[SubjectAverage]:
        rows = object_payload(await self._fetch(student, "averages", today, today))
        result = []
        try:
            for row in rows:
                periods = row.get("periods") or []
                current = next(
                    (
                        p
                        for p in periods
                        if p.get("start_iso")
                        and p.get("end_iso")
                        and iso_day(p["start_iso"]) <= today <= iso_day(p["end_iso"])
                    ),
                    None,
                )
                start = iso_day(current["start_iso"]) if current else None
                end = iso_day(current["end_iso"]) if current else None
                # Never confuse the all-year average with a current-period average.
                result.append(
                    SubjectAverage(
                        student_id=student.id,
                        subject_id=str(row["subject_id"]),
                        subject_name=str(row["subject_name"]),
                        period_id=f"{start}:{end}" if current else "unknown",
                        period_start=start,
                        period_end=end,
                        average=number(current.get("value")) if current else None,
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise MeshAPIError("API изменился: некорректная структура средних.") from None
        return result

    async def get_schedule(self, student: Student, start: date, end: date) -> list[dict[str, Any]]:
        return object_payload(await self._fetch(student, "schedule", start, end), key="response")
