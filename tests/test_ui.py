from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import CallbackQuery, Message, Update

from app.bot.ui import dispatcher
from app.mesh.models import Homework, Student
from app.repository.database import Database
from app.services.reports import ReportService
from app.services.selection import SelectionService


async def test_shared_access_child_selection_and_homework_commands(tmp_path: Path) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    students = [
        Student(id="1", profile_id="p", name="Анна"),
        Student(id="2", profile_id="p", name="Михаил"),
    ]
    await db.save_students(students)
    now = datetime(2026, 9, 18, 18, tzinfo=ZoneInfo("Europe/Moscow"))
    for student in students:
        await db.save_homework(
            student,
            [
                Homework(
                    id=student.id,
                    student_id=student.id,
                    subject_id="3",
                    subject_name="Математика",
                    lesson_date=date(2026, 9, 19),
                    text=f"Задание {student.name}",
                )
            ],
            "2026-09-19",
            "2026-09-19",
            now.isoformat(),
        )
    selection = SelectionService(db, frozenset({10, 20}))
    sync = SimpleNamespace(now=lambda: now, run=AsyncMock(return_value=True))
    settings = SimpleNamespace(allowed_chats=frozenset({10, 20}))
    dp = dispatcher(settings, sync, db, ReportService(db, now.tzinfo), selection)  # type: ignore[arg-type]
    bot = Bot("123456:fake_token_for_test")

    def update(text: str, chat: int = 10) -> Update:
        return Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=now,
                chat={"id": chat, "type": "private"},
                from_user={"id": chat, "is_bot": False, "first_name": "Test"},
                text=text,
            ),
        )

    try:
        with patch.object(Message, "answer", new_callable=AsyncMock) as answer:
            await dp.feed_update(bot, update("/tomorrow"))
            text = "".join(call.args[0] for call in answer.await_args_list)
            assert "Анна" in text and "Михаил" in text and "19.09" in text and "18.09" not in text
            assert answer.await_args.kwargs["parse_mode"] == "HTML"
            answer.reset_mock()
            with (
                patch.object(CallbackQuery, "answer", new_callable=AsyncMock),
                patch.object(
                    Message,
                    "edit_reply_markup",
                    new_callable=AsyncMock,
                ),
            ):
                await dp.feed_update(
                    bot,
                    Update(
                        update_id=2,
                        callback_query=CallbackQuery(
                            id="test",
                            from_user={"id": 10, "is_bot": False, "first_name": "Test"},
                            chat_instance="test",
                            message=update("menu").message,
                            data="choose:2",
                        ),
                    ),
                )
            scope = await selection.scope(10)
            assert scope and scope.student_ids == {"2"}
            # Selection survives a new service instance, while another chat keeps its own choice.
            persisted = await SelectionService(db, frozenset({10, 20})).scope(10)
            assert persisted and persisted.student_ids == {"2"}
            other = await selection.scope(20)
            assert other and other.student_ids == {"1", "2"}
            assert await selection.select(20, "1", now)
            assert await selection.select(20, "2", now)  # Shared rights to every child.
            assert not await selection.select(10, "unknown", now)
            answer.reset_mock()
            await dp.feed_update(bot, update("/hw_week"))
            text = "".join(call.args[0] for call in answer.await_args_list)
            assert "Михаил" in text and "Анна" not in text
            assert "19.09" in text and "20.09" in text and "18.09" not in text
            answer.reset_mock()
            await dp.feed_update(bot, update("/hw_next"))
            text = "".join(call.args[0] for call in answer.await_args_list)
            assert "21.09" in text and "27.09" in text and "19.09" not in text
            sync.run.reset_mock()
            answer.reset_mock()
            await dp.feed_update(bot, update("/today"))
            sync.run.assert_not_awaited()
            assert "ДЗ на сегодня убраны" in answer.await_args.args[0]
            answer.reset_mock()
            await dp.feed_update(bot, update("/tomorrow", chat=30))
            assert answer.await_args.args[0] == "Доступ запрещён."
    finally:
        await bot.session.close()
        await db.close()
