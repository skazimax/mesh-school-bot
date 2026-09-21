"""Per-child tables, MESH trimester means and weekly snapshot comparisons."""

import html
import textwrap
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.mesh.models import Student
from app.repository.database import Database
from app.services.report_images import GradeDocument, GradeRow


@dataclass(frozen=True)
class AverageChange:
    before: Decimal | None
    after: Decimal | None
    delta: Decimal | None
    direction: str


def average_change(before: Decimal | None, after: Decimal | None) -> AverageChange:
    if before is None or after is None:
        return AverageChange(before, after, None, "UNKNOWN")
    delta = (after - before).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return AverageChange(
        before, after, delta, "UP" if delta > 0 else "DOWN" if delta < 0 else "UNCHANGED"
    )


def change_text(change: AverageChange) -> str:
    if change.after is None:
        return "—"
    mean = f"{change.after:.2f}".replace(".", ",")
    if change.delta is None:
        return mean + " —"
    if change.delta == 0:
        return mean + " →"
    return (
        mean
        + " "
        + ("↑" if change.delta > 0 else "↓")
        + f"{abs(change.delta):.2f}".replace(".", ",")
    )


def day_start(day: date, timezone: ZoneInfo) -> str:
    return datetime.combine(day, time.min, timezone).isoformat()


def fives_needed(
    marks: list[dict[str, Any]],
    official_mean: Decimal | None,
    threshold: Decimal = Decimal("4.50"),
    new_weight: int = 1,
) -> int | None:
    """Estimate future weighted fives using complete, reconciled period marks."""
    if not threshold.is_finite() or not 1 <= threshold < 5 or new_weight <= 0:
        raise ValueError("Invalid target or future mark weight")
    if official_mean is None or not official_mean.is_finite():
        return None
    score, weight = Decimal(0), 0
    for mark in marks:
        if mark["numeric_value"] is None:
            continue
        value = Decimal(mark["numeric_value"])
        mark_weight = mark["weight"]
        if not value.is_finite() or not 1 <= value <= 5 or mark_weight <= 0:
            return None
        score += value * mark_weight
        weight += mark_weight
    # MESH rounds the displayed mean; allow only a rounding-sized discrepancy.
    if not weight or abs(score / weight - official_mean) > Decimal("0.01"):
        return None
    deficit = threshold * weight - score
    return max(
        0, int((deficit / ((5 - threshold) * new_weight)).to_integral_value(rounding=ROUND_CEILING))
    )


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def homework_dates(today: date, mode: str) -> tuple[date, date]:
    if mode == "tomorrow":
        tomorrow = today + timedelta(days=1)
        return tomorrow, tomorrow
    if mode == "remaining":
        return today + timedelta(days=1), week_start(today) + timedelta(days=6)
    if mode == "next":
        start = week_start(today) + timedelta(days=7)
        return start, start + timedelta(days=6)
    raise ValueError("unknown homework range")


def split_messages(text: str, limit: int = 3000) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    units = 0
    for line in text.splitlines(keepends=True):
        line_units = len(line.encode("utf-16-le")) // 2
        if current and units + line_units > limit:
            parts.append("".join(current))
            current, units = [], 0
        for char in line:
            size = len(char.encode("utf-16-le")) // 2
            if units + size > limit:
                parts.append("".join(current))
                current, units = [], 0
            current.append(char)
            units += size
    if current:
        parts.append("".join(current))
    return parts


def telegram_parts(text: str) -> list[str]:
    # Escape after splitting: each HTML message is independently valid.
    return ["<pre>" + html.escape(part) + "</pre>" for part in split_messages(text)]


def cells(values: tuple[str, ...], widths: tuple[int, ...]) -> list[str]:
    columns = [
        [
            line
            for paragraph in value.replace("\r", "").split("\n")
            for line in (textwrap.wrap(paragraph, width=width) or [""])
        ]
        for value, width in zip(values, widths, strict=True)
    ]
    return [
        " ".join(
            (column[index] if index < len(column) else "").ljust(width)
            for column, width in zip(columns, widths, strict=True)
        ).rstrip()
        for index in range(max(map(len, columns)))
    ]


class ReportService:
    def __init__(
        self,
        db: Database,
        timezone: ZoneInfo,
        five_threshold: Decimal = Decimal("4.50"),
        five_weight: int = 1,
    ) -> None:
        self.db, self.timezone = db, timezone
        self.five_threshold, self.five_weight = five_threshold, five_weight

    async def subject_fives(self, current: dict[str, Any], today: date, until: str) -> int | None:
        if (
            current["period_id"] == "unknown"
            or not current["period_start"]
            or not current["period_end"]
            or not current["period_start"] <= today.isoformat() <= current["period_end"]
        ):
            return None
        marks = await self.db.rows(
            "SELECT numeric_value,weight FROM marks WHERE student_id=? AND subject_id=? "
            "AND lesson_date>=? AND lesson_date<=? AND first_seen_at<=?",
            (
                current["student_id"],
                current["subject_id"],
                current["period_start"],
                min(today.isoformat(), current["period_end"]),
                until,
            ),
        )
        return fives_needed(
            marks,
            Decimal(current["average"]) if current["average"] is not None else None,
            self.five_threshold,
            self.five_weight,
        )

    async def students(self, student_ids: frozenset[str] | None) -> list[Student]:
        return [s for s in await self.db.students() if student_ids is None or s.id in student_ids]

    async def current_averages(self, student_id: str, until: str) -> list[dict[str, Any]]:
        return await self.db.rows(
            "SELECT s.* FROM subject_snapshots s WHERE student_id=? AND snapshot_at<=? "
            "AND snapshot_at=(SELECT MAX(t.snapshot_at) FROM subject_snapshots t "
            "WHERE t.student_id=s.student_id AND t.subject_id=s.subject_id AND t.snapshot_at<=?) "
            "ORDER BY subject_name",
            (student_id, until, until),
        )

    async def get_average_change(
        self, student_id: str, subject_id: str, since: str, until: str
    ) -> AverageChange:
        rows = await self.db.rows(
            "SELECT * FROM subject_snapshots WHERE student_id=? AND subject_id=? "
            "AND snapshot_at<=? ORDER BY snapshot_at DESC LIMIT 1",
            (student_id, subject_id, until),
        )
        if not rows or rows[0]["period_id"] == "unknown":
            return average_change(None, None)
        current = rows[0]
        previous = await self.db.rows(
            "SELECT average FROM subject_snapshots WHERE student_id=? AND subject_id=? "
            "AND period_id=? AND snapshot_at<=? ORDER BY snapshot_at DESC LIMIT 1",
            (student_id, subject_id, current["period_id"], since),
        )
        return average_change(
            Decimal(previous[0]["average"])
            if previous and previous[0]["average"] is not None
            else None,
            Decimal(current["average"]) if current["average"] is not None else None,
        )

    async def weekly_change(self, current: dict[str, Any], today: date) -> AverageChange:
        if (
            current["period_id"] == "unknown"
            or current["period_end"]
            and current["period_end"] < today.isoformat()
            or current["period_start"]
            and current["period_start"] > today.isoformat()
        ):
            return average_change(None, None)
        previous = await self.db.rows(
            "SELECT average FROM weekly_averages WHERE student_id=? AND subject_id=? "
            "AND week_start=? AND period_id=?",
            (
                current["student_id"],
                current["subject_id"],
                (week_start(today) - timedelta(days=7)).isoformat(),
                current["period_id"],
            ),
        )
        return average_change(
            Decimal(previous[0]["average"])
            if previous and previous[0]["average"] is not None
            else None,
            Decimal(current["average"]) if current["average"] is not None else None,
        )

    async def capture_weekly(
        self, today: date, until: str, student_ids: frozenset[str] | None = None
    ) -> None:
        values: list[tuple[Any, ...]] = []
        for student in await self.students(student_ids):
            for current in await self.current_averages(student.id, until):
                values.append(
                    (
                        student.id,
                        current["subject_id"],
                        week_start(today).isoformat(),
                        current["period_id"],
                        current["average"],
                        until,
                    )
                )
        async with self.db.lock:
            try:
                await self.db.db.executemany(
                    "INSERT INTO weekly_averages VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(student_id,subject_id,week_start) DO UPDATE SET "
                    "period_id=excluded.period_id,average=excluded.average,"
                    "snapshot_at=excluded.snapshot_at "
                    "WHERE excluded.snapshot_at>=weekly_averages.snapshot_at",
                    values,
                )
                await self.db.db.commit()
            except BaseException:
                await self.db.db.rollback()
                raise

    async def homework(
        self, day: date, *, stale: bool = False, student_ids: frozenset[str] | None = None
    ) -> str:
        return await self.homework_range(day, day, "ДЗ", stale=stale, student_ids=student_ids)

    async def homework_range(
        self,
        start: date,
        end: date,
        title: str,
        *,
        stale: bool = False,
        student_ids: frozenset[str] | None = None,
    ) -> str:
        if start > end:
            return "На этой неделе будущих дней не осталось. Выберите «Следующая неделя»."
        lines = []
        if stale:
            lines.append("⚠️ Сохранённые ДЗ: МЭШ недоступен.\n")
        weekdays = (
            "Понедельник",
            "Вторник",
            "Среда",
            "Четверг",
            "Пятница",
            "Суббота",
            "Воскресенье",
        )
        for student in await self.students(student_ids):
            lines.extend(textwrap.wrap(f"👤 {student.name}", width=33))
            lines.extend(textwrap.wrap(title, width=33))
            lines.append("")
            day = start
            while day <= end:
                lines.append(f"📅 {weekdays[day.weekday()]}, {day:%d.%m}")
                rows = await self.db.rows(
                    "SELECT * FROM homework WHERE student_id=? AND lesson_date=? "
                    "ORDER BY subject_name,mesh_homework_id",
                    (student.id, day.isoformat()),
                )
                if rows:
                    lines.extend(cells(("Предмет", "Задание"), (12, 20)))
                    lines.append("─" * 12 + " " + "─" * 20)
                    for row in rows:
                        lines.extend(
                            cells(
                                (row["subject_name"], row["text"] or "Текст не указан в МЭШ"),
                                (12, 20),
                            )
                        )
                else:
                    lines.append(
                        "В МЭШ заданий не найдено." if not stale else "В кэше заданий не найдено."
                    )
                lines.append("")
                day += timedelta(days=1)
        return "\n".join(lines).strip()

    async def grade_table(
        self,
        student: Student,
        marks: list[dict[str, Any]],
        title: str,
        today: date,
        until: str,
        *,
        all_subjects: bool,
    ) -> list[str]:
        lines = textwrap.wrap(f"👤 {student.name}", width=33) + textwrap.wrap(title, width=33)
        averages = {
            row["subject_id"]: row for row in await self.current_averages(student.id, until)
        }
        periods = sorted(
            {
                (row["period_start"], row["period_end"])
                for row in averages.values()
                if row["period_start"] and row["period_end"]
            }
        )
        for start, end in periods:
            lines.append(
                f"📊 Триместр {date.fromisoformat(start):%d.%m}–{date.fromisoformat(end):%d.%m}"
            )
        groups: dict[str, list[dict[str, Any]]] = {}
        for mark in marks:
            groups.setdefault(mark["subject_id"], []).append(mark)
        ids = set(groups) | (set(averages) if all_subjects else set())
        if not ids:
            return lines + [
                "Новых оценок нет." if not all_subjects else "Оценок и средних в МЭШ не найдено.",
                "",
            ]
        widths = (12, 5, 10, 3)
        lines.extend(cells(("Предмет", "Оценки", "Среднее", "До⑤"), widths))
        lines.append(" ".join("─" * width for width in widths))
        names = {
            subject: averages[subject]["subject_name"]
            if subject in averages
            else groups[subject][0]["subject_name"]
            for subject in ids
        }
        for subject in sorted(ids, key=lambda key: names[key]):
            values = " ".join(mark["value"] for mark in groups.get(subject, [])) or "—"
            change = (
                await self.weekly_change(averages[subject], today)
                if subject in averages
                else average_change(None, None)
            )
            needed = (
                await self.subject_fives(averages[subject], today, until)
                if subject in averages
                else None
            )
            lines.extend(
                cells(
                    (
                        names[subject],
                        values,
                        change_text(change),
                        str(needed) if needed is not None else "—",
                    ),
                    widths,
                )
            )
        target = f"{self.five_threshold:.2f}".replace(".", ",")
        lines.extend(
            textwrap.wrap(
                f"До⑤: пятёрки веса {self.five_weight} до {target}. Ориентир, не итоговая оценка.",
                width=33,
            )
        )
        lines.append("")
        return lines

    async def grade_documents(
        self,
        kind: str,
        today: date,
        until: str,
        student_ids: frozenset[str] | None = None,
        *,
        since: str | None = None,
        stale: bool = False,
    ) -> list[GradeDocument]:
        """Structured view for images, independent of the retained text renderer."""
        if kind not in {"daily", "weekly", "monthly"}:
            raise ValueError("Unknown grade report kind")
        start = week_start(today) if kind == "weekly" else today.replace(day=1)
        title = (
            f"Новые оценки · {today:%d.%m.%Y}"
            if kind == "daily"
            else f"{'Неделя' if kind == 'weekly' else 'Месяц'} {start:%d.%m}–{today:%d.%m.%Y}"
        )
        target = f"{self.five_threshold:.2f}".replace(".", ",")
        documents = []
        for student in await self.students(student_ids):
            if kind == "daily":
                marks = await self.db.rows(
                    "SELECT * FROM marks WHERE student_id=? AND initial_import=0 "
                    "AND first_seen_at>=? AND first_seen_at<? ORDER BY subject_name,first_seen_at",
                    (student.id, since or day_start(today, self.timezone), until),
                )
                if not marks:
                    continue
            else:
                marks = await self.db.rows(
                    "SELECT * FROM marks WHERE student_id=? AND lesson_date BETWEEN ? AND ? "
                    "ORDER BY subject_name,lesson_date,mesh_mark_id",
                    (student.id, start.isoformat(), today.isoformat()),
                )
            averages = {
                row["subject_id"]: row for row in await self.current_averages(student.id, until)
            }
            groups: dict[str, list[dict[str, Any]]] = {}
            for mark in marks:
                groups.setdefault(mark["subject_id"], []).append(mark)
            ids = set(groups) | (set(averages) if kind != "daily" else set())
            rows = []
            for subject in ids:
                current = averages.get(subject)
                name = current["subject_name"] if current else groups[subject][0]["subject_name"]
                change = (
                    await self.weekly_change(current, today)
                    if current
                    else average_change(None, None)
                )
                needed = await self.subject_fives(current, today, until) if current else None
                change_parts = change_text(change).split(" ", 1)
                rows.append(
                    GradeRow(
                        subject=name,
                        marks=" · ".join(mark["value"] for mark in groups.get(subject, [])) or "—",
                        average=change_parts[0],
                        delta=change_parts[1] if len(change_parts) > 1 else "—",
                        direction=change.direction,
                        needed=str(needed) if needed is not None else "—",
                    )
                )
            periods = sorted(
                {
                    f"{date.fromisoformat(row['period_start']):%d.%m}–"
                    f"{date.fromisoformat(row['period_end']):%d.%m}"
                    for row in averages.values()
                    if row["period_start"] and row["period_end"]
                }
            )
            documents.append(
                GradeDocument(
                    student=student.name,
                    title=title,
                    periods=periods,
                    rows=sorted(rows, key=lambda row: row.subject),
                    marks_label={"daily": "Новые", "weekly": "За неделю", "monthly": "За месяц"}[
                        kind
                    ],
                    legend=f"До 5 — пятёрки веса {self.five_weight} до среднего {target}",
                    warning="Сохранённые данные: МЭШ недоступен" if stale else "",
                    empty_text="Новых оценок нет."
                    if kind == "daily"
                    else "Оценок и средних в МЭШ не найдено.",
                )
            )
        return documents

    async def daily(
        self,
        today: date,
        until: str,
        since: str | None = None,
        student_ids: frozenset[str] | None = None,
    ) -> tuple[str, int]:
        start = since or day_start(today, self.timezone)
        lines, total = [], 0
        for student in await self.students(student_ids):
            rows = await self.db.rows(
                "SELECT * FROM marks WHERE student_id=? AND initial_import=0 "
                "AND first_seen_at>=? AND first_seen_at<? ORDER BY subject_name,first_seen_at",
                (student.id, start, until),
            )
            if not rows:
                continue
            lines.extend(
                await self.grade_table(
                    student, rows, f"Новые оценки · {today:%d.%m}", today, until, all_subjects=False
                )
            )
            total += len(rows)
        return "\n".join(lines).strip(), total

    async def period(
        self,
        start: date,
        end: date,
        title: str,
        until: str,
        student_ids: frozenset[str] | None = None,
    ) -> str:
        lines = []
        for student in await self.students(student_ids):
            rows = await self.db.rows(
                "SELECT * FROM marks WHERE student_id=? AND lesson_date BETWEEN ? AND ? "
                "ORDER BY subject_name,lesson_date,mesh_mark_id",
                (student.id, start.isoformat(), end.isoformat()),
            )
            lines.extend(
                await self.grade_table(
                    student,
                    rows,
                    f"{title} {start:%d.%m}–{end:%d.%m.%Y}",
                    end,
                    until,
                    all_subjects=True,
                )
            )
        return "\n".join(lines).strip()


class WeeklyReportService:
    def __init__(self, reports: ReportService) -> None:
        self.reports = reports

    async def render(
        self,
        today: date,
        until: str,
        student_ids: frozenset[str] | None = None,
        *,
        capture: bool = True,
    ) -> str:
        result = await self.reports.period(week_start(today), today, "Неделя", until, student_ids)
        if capture:
            await self.reports.capture_weekly(today, until, student_ids)
        return result


class MonthlyReportService:
    def __init__(self, reports: ReportService) -> None:
        self.reports = reports

    async def render(
        self, today: date, until: str, student_ids: frozenset[str] | None = None
    ) -> str:
        return await self.reports.period(today.replace(day=1), today, "Месяц", until, student_ids)
