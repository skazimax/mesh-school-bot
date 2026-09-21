from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Message, Update

from app.bot.ui import dispatcher
from app.mesh.models import Mark, Student, SubjectAverage
from app.repository.database import Database
from app.services.reports import ReportService
from app.services.selection import SelectionService


@pytest.mark.parametrize("report_format", ["text", "image"])
async def test_group_commands_and_callbacks_send_to_actor_and_private_members_can_continue(
    tmp_path: Path,
    report_format: str,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    now = datetime(2026, 9, 18, 18, tzinfo=ZoneInfo("Europe/Moscow"))
    students = [
        Student(id="1", profile_id="p", name="Анна"),
        Student(id="2", profile_id="p", name="Михаил"),
    ]
    await db.save_students(students)
    for student in students:
        await db.save_marks(
            student,
            [
                Mark(
                    id="m",
                    student_id=student.id,
                    subject_id="3",
                    subject_name="Математика",
                    value="5",
                    numeric_value=5,
                    lesson_date=date(2026, 9, 17),
                )
            ],
            [
                SubjectAverage(
                    student_id=student.id,
                    subject_id="3",
                    subject_name="Математика",
                    average=5,
                    period_id="term",
                    period_start=date(2026, 9, 1),
                    period_end=date(2026, 11, 30),
                )
            ],
            now.isoformat(),
            False,
        )
    selection = SelectionService(db, frozenset({10}))
    await selection.bind_channel(-1001234, now)
    settings = SimpleNamespace(allowed_chats=frozenset({10}), report_format=report_format)
    sync = SimpleNamespace(now=lambda: now, run=AsyncMock(return_value=True))
    dp = dispatcher(settings, sync, db, ReportService(db, now.tzinfo), selection)  # type: ignore[arg-type]
    bot = Bot("123456:fake_token_for_test")
    message = Message(
        message_id=1,
        date=now,
        chat={"id": -1001234, "type": "supergroup"},
        from_user={"id": 30, "is_bot": False, "first_name": "Member"},
        text="/week",
        message_thread_id=42,
        is_topic_message=True,
    )
    sent = []

    async def transport(self, method, **kwargs):  # type: ignore[no-untyped-def]
        sent.append(method)
        return True

    def click(value: str) -> Update:
        return Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="click",
                from_user=message.from_user,
                chat_instance="test",
                message=message,
                data=value,
            ),
        )

    try:
        with (
            patch.object(Bot, "__call__", new=transport),
            patch.object(
                Bot,
                "get_chat_member",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(status="member"),
            ) as member,
        ):
            await dp.feed_update(bot, Update(update_id=1, message=message))
            reports = [item for item in sent if isinstance(item, (SendMessage, SendPhoto))]
            assert len(reports) == (2 if report_format == "image" else 1)
            assert all(item.chat_id == 30 and item.message_thread_id is None for item in reports)
            assert all(
                isinstance(item, SendPhoto if report_format == "image" else SendMessage)
                for item in reports
            )
            assert all(item.reply_markup and item.reply_markup.remove_keyboard for item in reports)
            sent.clear()
            await dp.feed_update(bot, click("choose:2"))
            own = await selection.scope(30, shared_member=True)
            common = await selection.scope(-1001234)
            assert own and own.student_ids == {"2"}
            assert common and common.student_ids == {"1", "2"}
            sent.clear()
            await dp.feed_update(bot, click("do:tomorrow"))
            homework = [item for item in sent if isinstance(item, SendMessage)]
            assert len(homework) == 1 and homework[0].chat_id == 30
            assert "Михаил" in homework[0].text and "Анна" not in homework[0].text
            assert not any(isinstance(item, SendPhoto) for item in sent)
            sent.clear()
            private = message.model_copy(
                update={
                    "chat": message.chat.model_copy(update={"id": 30, "type": "private"}),
                    "text": "/month",
                }
            )
            await dp.feed_update(bot, Update(update_id=3, message=private))
            reports = [item for item in sent if isinstance(item, (SendMessage, SendPhoto))]
            assert len(reports) == 1 and reports[0].chat_id == 30
            sent.clear()
            member.return_value = SimpleNamespace(status="left")
            await dp.feed_update(bot, Update(update_id=4, message=private))
            assert len(sent) == 1 and sent[0].text == "Доступ запрещён."
    finally:
        await bot.session.close()
        await db.close()


@pytest.mark.parametrize("not_started", [False, True])
async def test_not_started_or_blocked_dm_has_one_group_hint_and_no_group_report(
    tmp_path: Path,
    not_started: bool,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    await db.save_students([Student(id="1", profile_id="p", name="Анна")])
    now = datetime(2026, 9, 18, 18, tzinfo=ZoneInfo("Europe/Moscow"))
    selection = SelectionService(db, frozenset({10}))
    await selection.bind_channel(-1001234, now)
    settings = SimpleNamespace(allowed_chats=frozenset({10}), report_format="text")
    sync = SimpleNamespace(now=lambda: now, run=AsyncMock(return_value=True))
    dp = dispatcher(settings, sync, db, ReportService(db, now.tzinfo), selection)  # type: ignore[arg-type]
    bot = Bot("123456:fake_token_for_test")
    message = Message(
        message_id=1,
        date=now,
        chat={"id": -1001234, "type": "group"},
        from_user={"id": 10, "is_bot": False, "first_name": "Test"},
        text="/tomorrow",
    )
    grouped = []

    async def transport(self, method, **kwargs):  # type: ignore[no-untyped-def]
        if method.chat_id == 10:
            if not_started:
                raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
            raise TelegramForbiddenError(method=method, message="Forbidden")
        grouped.append(method)
        return True

    try:
        with patch.object(Bot, "__call__", new=transport):
            await dp.feed_update(bot, Update(update_id=1, message=message))
            await dp.feed_update(bot, Update(update_id=2, message=message))
            assert len(grouped) == 1
            assert "Старт" in grouped[0].text
            assert "ДЗ" not in grouped[0].text and "Анна" not in grouped[0].text
    finally:
        await bot.session.close()
        await db.close()
