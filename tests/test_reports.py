from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.mesh.models import Homework, Mark, Student, SubjectAverage
from app.repository.database import Database
from app.services.reports import (
    MonthlyReportService,
    ReportService,
    WeeklyReportService,
    average_change,
    change_text,
    fives_needed,
    homework_dates,
    split_messages,
    telegram_parts,
)


@pytest.mark.parametrize(
    "before,after,direction,text",
    [
        ("4.42", "4.50", "UP", "4,50 ↑0,08"),
        ("4.31", "4.24", "DOWN", "4,24 ↓0,07"),
        ("4.50", "4.50", "UNCHANGED", "4,50 →"),
    ],
)
def test_average_changes(before: str, after: str, direction: str, text: str) -> None:
    change = average_change(Decimal(before), Decimal(after))
    assert change.direction == direction
    assert text in change_text(change)


@pytest.mark.parametrize(
    "values,weights,mean,target,future_weight,expected",
    [
        ([4, 4], [1, 1], "4", "4.50", 1, 2),
        ([3, 5], [2, 1], "3.67", "4.50", 1, 5),
        ([3, 5], [2, 1], "3.67", "4.50", 2, 3),
        ([4, 5], [1, 1], "4.50", "4.50", 1, 0),
        ([5], [1], "5", "4.50", 1, 0),
        ([4, 4], [1, 1], "4", "4.60", 1, 3),
        ([4, 4], [1, 1], "4.25", "4.50", 1, None),
        ([], [], "4.50", "4.50", 1, None),
        ([4], [0], "4", "4.50", 1, None),
    ],
)
def test_fives_needed(values, weights, mean, target, future_weight, expected) -> None:  # type: ignore[no-untyped-def]
    marks = [
        {"numeric_value": str(value), "weight": weight}
        for value, weight in zip(values, weights, strict=True)
    ]
    assert fives_needed(marks, Decimal(mean), Decimal(target), future_weight) == expected


async def test_fives_use_whole_current_trimester_and_reconcile_mesh(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    # Earlier-period grades and another student's grades cannot affect the estimate.
    marks = [
        Mark(
            id="older",
            student_id="1",
            subject_id="8",
            subject_name="Биология",
            value="4",
            numeric_value=Decimal(4),
            weight=2,
            lesson_date=date(2026, 9, 2),
        ),
        Mark(
            id="recent",
            student_id="1",
            subject_id="8",
            subject_name="Биология",
            value="5",
            numeric_value=Decimal(5),
            lesson_date=date(2026, 9, 17),
        ),
        Mark(
            id="previous-term",
            student_id="1",
            subject_id="8",
            subject_name="Биология",
            value="2",
            numeric_value=Decimal(2),
            weight=10,
            lesson_date=date(2026, 8, 31),
        ),
    ]
    average = SubjectAverage(
        student_id="1",
        subject_id="8",
        subject_name="Биология",
        period_id="term",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 11, 30),
        average=Decimal("4.33"),
    )
    until = "2026-09-18T19:00:00+03:00"
    await db.save_marks(student, marks, [average], "2026-09-18T18:00:00+03:00", False)
    current = next(
        row for row in await reports.current_averages("1", until) if row["subject_id"] == "8"
    )
    assert await reports.subject_fives(current, date(2026, 9, 18), until) == 1
    text = await WeeklyReportService(reports).render(date(2026, 9, 18), until)
    assert "Биология     5     4,33 —     1" in text
    documents = await reports.grade_documents("weekly", date(2026, 9, 18), until)
    biology = next(row for row in documents[0].rows if row.subject == "Биология")
    assert biology.marks == "5" and biology.needed == "1" and biology.average == "4,33"
    assert max(map(len, text.splitlines())) <= 33
    current["average"] = "4.50"
    assert await reports.subject_fives(current, date(2026, 9, 18), until) is None
    current["period_start"] = None
    assert await reports.subject_fives(current, date(2026, 9, 18), until) is None


@pytest.mark.parametrize("target,weight", [("5", 1), ("NaN", 1), ("4.50", 0)])
def test_fives_reject_invalid_settings(target: str, weight: int) -> None:
    with pytest.raises(ValueError):
        fives_needed([], Decimal(4), Decimal(target), weight)


@pytest.fixture
async def data(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = Database(tmp_path / "bot.db")
    await db.open()
    student = Student(id="1", profile_id="2", name="Ребёнок")
    await db.save_students([student])
    baseline = SubjectAverage(
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        average=Decimal("4.42"),
        period_id="term",
    )
    history = Mark(
        id="1",
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        value="4",
        numeric_value=Decimal(4),
        lesson_date=date(2026, 9, 14),
    )
    await db.save_marks(student, [history], [baseline], "2026-09-13T20:00:00+03:00", True)
    reports = ReportService(db, ZoneInfo("Europe/Moscow"))
    await reports.capture_weekly(date(2026, 9, 13), "2026-09-13T21:00:00+03:00")
    baseline.average = Decimal("4.62")
    late = Mark(
        id="2",
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        value="5",
        numeric_value=Decimal(5),
        weight=2,
        lesson_date=date(2026, 9, 17),
    )
    await db.save_marks(student, [late], [baseline], "2026-09-18T13:00:00+03:00", False)
    await db.save_homework(
        student,
        [
            Homework(
                id="1",
                student_id="1",
                subject_id="3",
                subject_name="Математика",
                lesson_date=date(2026, 9, 18),
                text="№125",
            )
        ],
        "2026-09-18",
        "2026-09-19",
        "2026-09-18T13:00:00+03:00",
    )
    yield db, reports, student
    await db.close()


async def test_daily_report_uses_first_seen_and_excludes_bootstrap(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    text, count = await reports.daily(date(2026, 9, 18), "2026-09-18T19:00:00+03:00")
    assert count == 1
    assert "Математика   5" in text
    assert "4,62 ↑0,20" in text
    assert "Математика   4" not in text
    assert "Получено оценок" not in text
    documents = await reports.grade_documents(
        "daily", date(2026, 9, 18), "2026-09-18T19:00:00+03:00"
    )
    assert len(documents) == 1
    assert documents[0].rows[0].marks == "5"
    assert documents[0].rows[0].average == "4,62"
    assert documents[0].rows[0].delta == "↑0,20"
    other = Student(id="2", profile_id="2", name="Другой ребёнок")
    await db.save_students([student, other])
    text, count = await reports.daily(date(2026, 9, 18), "2026-09-18T19:00:00+03:00")
    documents = await reports.grade_documents(
        "daily", date(2026, 9, 18), "2026-09-18T19:00:00+03:00"
    )
    assert count == 1 and "Другой ребёнок" not in text
    assert [document.student for document in documents] == ["Ребёнок"]


async def test_weekly_report_uses_mesh_trimester_mean_and_previous_week(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    text = await WeeklyReportService(reports).render(date(2026, 9, 18), "2026-09-18T19:00:00+03:00")
    assert "14.09–18.09.2026" in text
    assert "Математика   4 5" in text
    assert "4,62 ↑0,20" in text
    assert "Среднее полученных" not in text
    assert "Получено оценок" not in text


async def test_monthly_report_does_not_invent_missing_baseline(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    async with db.lock:
        await db.db.execute("DELETE FROM weekly_averages")
        await db.db.commit()
    text = await MonthlyReportService(reports).render(
        date(2026, 9, 18), "2026-09-18T19:00:00+03:00"
    )
    assert "01.09–18.09.2026" in text
    assert "4,62 —" in text
    assert "↑" not in text


async def test_homework_today_and_tomorrow(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    assert "№125" in await reports.homework(date(2026, 9, 18))
    assert "В МЭШ заданий не найдено" in await reports.homework(date(2026, 9, 19))
    assert "В кэше заданий не найдено" in await reports.homework(date(2026, 9, 19), stale=True)


async def test_average_delta_never_crosses_terms(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    average = SubjectAverage(
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        average=Decimal("3.50"),
        period_id="next-term",
    )
    await db.save_marks(student, [], [average], "2026-09-18T15:00:00+03:00", False)
    change = await reports.get_average_change(
        "1", "3", "2026-09-18T00:00:00+03:00", "2026-09-18T19:00:00+03:00"
    )
    assert change.before is None
    assert change.after == Decimal("3.50")
    assert change.delta is None


def test_telegram_chunks_respect_utf16_limits() -> None:
    text = "📚" * 5000 + " Домашнее задание"
    parts = split_messages(text)
    assert "".join(parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)


@pytest.mark.parametrize(
    "today,mode,start,end",
    [
        ("2026-09-18", "tomorrow", "2026-09-19", "2026-09-19"),
        ("2026-09-18", "remaining", "2026-09-19", "2026-09-20"),
        ("2026-09-20", "remaining", "2026-09-21", "2026-09-20"),
        ("2026-09-20", "next", "2026-09-21", "2026-09-27"),
        ("2026-12-31", "next", "2027-01-04", "2027-01-10"),
    ],
)
def test_homework_calendar_ranges(today: str, mode: str, start: str, end: str) -> None:
    assert homework_dates(date.fromisoformat(today), mode) == (
        date.fromisoformat(start),
        date.fromisoformat(end),
    )


async def test_reports_isolate_children_and_include_subjects_without_new_marks(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    other = Student(id="2", profile_id="2", name="Другой ребёнок")
    await db.save_students([student, other])
    await db.save_homework(
        other,
        [
            Homework(
                id="private",
                student_id="2",
                subject_id="9",
                subject_name="История",
                lesson_date=date(2026, 9, 19),
                text="Чужое задание",
            )
        ],
        "2026-09-19",
        "2026-09-19",
        "2026-09-18T18:00:00+03:00",
    )
    await db.save_marks(
        student,
        [],
        [
            SubjectAverage(
                student_id="1",
                subject_id="8",
                subject_name="Биология",
                period_id="term",
                average=Decimal("4.75"),
            )
        ],
        "2026-09-18T18:00:00+03:00",
        False,
    )
    text = await WeeklyReportService(reports).render(
        date(2026, 9, 18),
        "2026-09-18T19:00:00+03:00",
        frozenset({"1"}),
    )
    assert "Другой ребёнок" not in text
    assert "Биология     —     4,75 —" in text
    homework = await reports.homework_range(
        date(2026, 9, 19),
        date(2026, 9, 20),
        "ДЗ",
        student_ids=frozenset({"1"}),
    )
    assert "Чужое задание" not in homework
    assert "Другой ребёнок" not in homework
    assert "19.09" in homework and "20.09" in homework


async def test_weekly_dynamics_do_not_cross_trimester_or_skip_missing_week(data) -> None:  # type: ignore[no-untyped-def]
    db, reports, student = data
    current = (await reports.current_averages("1", "2026-09-18T19:00:00+03:00"))[0]
    assert (await reports.weekly_change(current, date(2026, 9, 18))).delta == Decimal("0.20")
    current["period_id"] = "next-term"
    assert (await reports.weekly_change(current, date(2026, 9, 18))).delta is None
    current["period_id"] = "term"
    assert (await reports.weekly_change(current, date(2026, 10, 2))).delta is None


def test_html_tables_escape_provider_content_and_keep_each_chunk_valid() -> None:
    text = "<b>текст & задание</b>" * 500 + "📚" * 2000
    parts = telegram_parts(text)
    import html

    assert "".join(html.unescape(part[5:-6]) for part in parts) == text
    assert all(part.startswith("<pre>") and part.endswith("</pre>") for part in parts)
    assert all(len(html.unescape(part[5:-6]).encode("utf-16-le")) // 2 <= 3000 for part in parts)
