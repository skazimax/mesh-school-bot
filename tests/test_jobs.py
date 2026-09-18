from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.bot.ui import authorized
from app.jobs.scheduler import Scheduler
from app.mesh.models import Mark, Student, SubjectAverage
from app.repository.database import Database
from app.services.reports import ReportService
from app.services.selection import SelectionService


@pytest.mark.parametrize(
    "chat_type,chat_id,user_id,expected",
    [
        ("private", 10, 10, True),
        ("private", 11, 11, False),
        ("group", 10, 10, False),
        ("private", 10, 11, False),
    ],
)
def test_allow_list(chat_type: str, chat_id: int, user_id: int, expected: bool) -> None:
    message = SimpleNamespace(
        chat=SimpleNamespace(type=chat_type, id=chat_id), from_user=SimpleNamespace(id=user_id)
    )
    assert authorized(message, frozenset({10})) == expected  # type: ignore[arg-type]


async def test_partial_delivery_resumes_without_resending_sent_parts(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    bot = SimpleNamespace(send_message=AsyncMock())
    scheduler = Scheduler(
        SimpleNamespace(),
        SimpleNamespace(),  # type: ignore[arg-type]
        ReportService(db, ZoneInfo("Europe/Moscow")),
        db,
        bot,
    )  # type: ignore[arg-type]
    now = "2026-09-18T19:00:00+03:00"
    await db.prepare_delivery("daily:test:10", ["first", "second"], now)
    await db.delivery_progress("daily:test:10", 1, False, now)
    await scheduler.send("daily:test:10", 10, "new data must not replace frozen report", now)
    bot.send_message.assert_awaited_once_with(10, "<pre>second</pre>", parse_mode="HTML")
    assert await scheduler.completed("daily:test:10")
    await scheduler.send("daily:test:10", 10, "another report", now)
    assert bot.send_message.await_count == 1
    await db.close()


async def test_friday_reports_daily_then_weekly_with_month_and_no_duplicates(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    now = datetime(2026, 9, 18, 19, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    settings = SimpleNamespace(
        allowed_chats=frozenset({10}),
        report_time=time(19),
        timezone=now.tzinfo,
        send_empty_daily=False,
    )
    sync = SimpleNamespace(run=AsyncMock(return_value=True), now=lambda: now)
    reports = SimpleNamespace(
        daily=AsyncMock(return_value=("daily", 1)),
        period=AsyncMock(return_value="period report"),
        capture_weekly=AsyncMock(),
    )
    bot = SimpleNamespace(send_message=AsyncMock())
    scheduler = Scheduler(settings, sync, reports, db, bot)  # type: ignore[arg-type]
    await scheduler.report_jobs(now)
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[0].args == (10, "<pre>daily</pre>")
    assert bot.send_message.await_args_list[1].args == (
        10,
        "<pre>period report\n\nperiod report</pre>",
    )
    assert reports.period.await_count == 2
    assert await db.state("daily_cutoff:10") == now.isoformat()
    await scheduler.report_jobs(now)
    assert bot.send_message.await_count == 2
    sync.run.assert_awaited_once()
    await db.close()


async def test_report_api_failure_throttles_retries(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    now = datetime(2026, 9, 18, 19, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    settings = SimpleNamespace(allowed_chats=frozenset({10}), report_time=time(19))
    sync = SimpleNamespace(run=AsyncMock(return_value=False))
    bot = SimpleNamespace(send_message=AsyncMock())
    scheduler = Scheduler(settings, sync, SimpleNamespace(), db, bot)  # type: ignore[arg-type]
    await scheduler.report_jobs(now)
    await scheduler.report_jobs(now)
    sync.run.assert_awaited_once()
    bot.send_message.assert_not_awaited()
    await db.close()


async def test_automatic_reports_only_go_to_connected_channel_and_use_its_selection(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    students = [
        Student(id="1", profile_id="p", name="Анна"),
        Student(id="2", profile_id="p", name="Михаил"),
    ]
    await db.save_students(students)
    now = datetime(2026, 9, 18, 19, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    for student in students:
        await db.save_marks(
            student,
            [
                Mark(
                    id=student.id,
                    student_id=student.id,
                    subject_id="3",
                    subject_name="Математика",
                    value="5",
                    lesson_date=date(2026, 9, 18),
                )
            ],
            [
                SubjectAverage(
                    student_id=student.id,
                    subject_id="3",
                    subject_name="Математика",
                    period_id="term",
                    average="4.62",
                )
            ],
            "2026-09-18T18:00:00+03:00",
            False,
        )
    selection = SelectionService(db, frozenset({10, 20}))
    await selection.select(10, "1", now)
    await selection.select(20, "2", now)
    settings = SimpleNamespace(
        allowed_chats=frozenset({10, 20}),
        report_time=time(19),
        timezone=now.tzinfo,
        send_empty_daily=False,
    )
    sync = SimpleNamespace(run=AsyncMock(return_value=True), now=lambda: now)
    bot = SimpleNamespace(send_message=AsyncMock())
    reports = ReportService(db, now.tzinfo)  # type: ignore[arg-type]
    scheduler = Scheduler(settings, sync, reports, db, bot, selection)  # type: ignore[arg-type]
    await scheduler.report_jobs(now)
    bot.send_message.assert_not_awaited()  # No channel: no private fallback.
    sync.run.assert_not_awaited()
    await selection.bind_channel(-1001234, now)
    await selection.select(-1001234, "2", now)
    await scheduler.report_jobs(now)
    assert bot.send_message.await_count == 2
    for call in bot.send_message.await_args_list:
        chat, content = call.args
        assert chat == -1001234
        assert "Анна" not in content and "Михаил" in content
        assert call.kwargs["parse_mode"] == "HTML"
        assert call.kwargs["reply_markup"].inline_keyboard
    assert len(await db.rows("SELECT * FROM weekly_averages")) == 1
    await scheduler.report_jobs(now)
    assert bot.send_message.await_count == 2
    await db.close()
