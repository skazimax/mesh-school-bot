import json
from datetime import date, datetime, time
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from app.jobs.scheduler import Scheduler
from app.mesh.models import Mark, Student, SubjectAverage
from app.repository.database import Database
from app.services.report_images import GradeDocument, GradeRow, render_image
from app.services.reports import ReportService
from app.services.selection import SelectionService


def sample(name: str = "Анна", subjects: int = 15) -> GradeDocument:
    return GradeDocument(
        student=name,
        title="Неделя 14.09–18.09.2026",
        periods=["01.09–30.11"],
        rows=[
            GradeRow(f"Предмет {i}", "5 · 4 · 5", "4,40", "↑0,12", "UP", "3")
            for i in range(subjects)
        ],
        marks_label="За неделю",
        legend="До 5 — пятёрки веса 1 до среднего 4,50",
    )


def test_compact_png_fits_all_subjects_and_round_trips_frozen_data() -> None:
    document = sample()
    assert GradeDocument.thaw(document.freeze()) == document
    with Image.open(BytesIO(render_image(document))) as image:
        assert image.format == "PNG"
        assert image.width == 1200
        assert 1300 < image.height < 1800
        # Last row and legend occupy the canvas, with space before the bottom edge.
        assert image.getpixel((40, image.height - 10)) == (255, 255, 255)


def test_long_cyrillic_names_and_grades_expand_instead_of_clipping() -> None:
    short = sample(subjects=1)
    long = GradeDocument(
        student="Очень длинное вымышленное имя ученика для проверки переноса",
        title=short.title,
        periods=short.periods,
        marks_label=short.marks_label,
        legend=short.legend,
        warning="Сохранённые данные: МЭШ недоступен",
        rows=[
            GradeRow(
                "Вероятность и статистика с дополнительными занятиями",
                " · ".join(["5"] * 30),
                "—",
                "—",
                "UNKNOWN",
                "—",
            )
        ],
    )
    with (
        Image.open(BytesIO(render_image(short))) as a,
        Image.open(BytesIO(render_image(long))) as b,
    ):
        assert b.height > a.height + 100
        assert b.width == a.width


async def test_frozen_image_delivery_resumes_and_ignores_changed_format(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    settings = SimpleNamespace(report_format="image", report_font_path=None)
    scheduler = Scheduler(
        settings, SimpleNamespace(), ReportService(db, ZoneInfo("Europe/Moscow")), db, bot
    )  # type: ignore[arg-type]
    now = "2026-09-18T19:00:00+03:00"
    original = [sample("Анна", 1), sample("Михаил", 1)]
    try:
        bot.send_photo.side_effect = [None, RuntimeError("retry")]
        with pytest.raises(RuntimeError):
            await scheduler.send("weekly:test:10", 10, "text", now, documents=original)
        saved = (await db.rows("SELECT * FROM report_delivery"))[0]
        assert saved["next_part"] == 1 and not saved["complete"]
        bot.send_photo.reset_mock(side_effect=True)
        settings.report_format = "text"
        await scheduler.send("weekly:test:10", 10, "changed data", now)
        bot.send_photo.assert_awaited_once()
        assert bot.send_photo.await_args.args[1].data == render_image(original[1])
        bot.send_message.assert_not_awaited()
        assert await scheduler.completed("weekly:test:10")
        await scheduler.send("weekly:test:10", 10, "changed data", now)
        assert bot.send_photo.await_count == 1
    finally:
        await db.close()


async def test_friday_automatic_images_keep_channel_destination_and_delivery_progress(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    student = Student(id="1", profile_id="p", name="Анна")
    await db.save_students([student])
    now = datetime(2026, 9, 18, 19, tzinfo=ZoneInfo("Europe/Moscow"))
    await db.save_marks(
        student,
        [
            Mark(
                id="1",
                student_id="1",
                subject_id="3",
                subject_name="Математика",
                value="5",
                numeric_value=5,
                lesson_date=date(2026, 9, 18),
            )
        ],
        [
            SubjectAverage(
                student_id="1",
                subject_id="3",
                subject_name="Математика",
                average=5,
                period_id="term",
                period_start=date(2026, 9, 1),
                period_end=date(2026, 11, 30),
            )
        ],
        "2026-09-18T18:00:00+03:00",
        False,
    )
    selection = SelectionService(db, frozenset({10}))
    await selection.bind_channel(-1001234, now)
    settings = SimpleNamespace(
        allowed_chats=frozenset({10}),
        report_time=time(19),
        timezone=now.tzinfo,
        send_empty_daily=False,
        report_format="image",
    )
    sync = SimpleNamespace(run=AsyncMock(return_value=True), now=lambda: now)
    bot = SimpleNamespace(send_message=AsyncMock(), send_photo=AsyncMock())
    scheduler = Scheduler(settings, sync, ReportService(db, now.tzinfo), db, bot, selection)  # type: ignore[arg-type]
    try:
        await scheduler.report_jobs(now)
        assert bot.send_photo.await_count == 3
        assert all(call.args[0] == -1001234 for call in bot.send_photo.await_args_list)
        bot.send_message.assert_not_awaited()
        deliveries = await db.rows("SELECT * FROM report_delivery ORDER BY key")
        assert len(deliveries) == 2 and all(row["complete"] for row in deliveries)
        assert len(json.loads(deliveries[1]["content"])) == 2
        assert await db.state("daily_cutoff:-1001234") == now.isoformat()
        assert len(await db.rows("SELECT * FROM weekly_averages")) == 1
        await scheduler.report_jobs(now)
        assert bot.send_photo.await_count == 3
    finally:
        await db.close()
