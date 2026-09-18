from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, Message, Update

from app.bot.ui import dispatcher
from app.mesh.models import Student
from app.repository.database import Database
from app.services.reports import ReportService
from app.services.selection import SelectionService


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_group_start_connects_and_commands_and_buttons_use_shared_chat(
    tmp_path: Path,
    chat_type: str,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    await db.save_students([Student(id="1", profile_id="p", name="Анна")])
    now = datetime(2026, 9, 18, 18, tzinfo=ZoneInfo("Europe/Moscow"))
    selection = SelectionService(db, frozenset({10}))
    sync = SimpleNamespace(now=lambda: now, run=AsyncMock(return_value=True))
    settings = SimpleNamespace(allowed_chats=frozenset({10}))
    dp = dispatcher(settings, sync, db, ReportService(db, now.tzinfo), selection)  # type: ignore[arg-type]
    bot = Bot("123456:fake_token_for_test")
    message = Message(
        message_id=1,
        date=now,
        chat={"id": -1001234, "type": chat_type},
        from_user={"id": 10, "is_bot": False, "first_name": "Test"},
        text="/start@example_school_bot",
    )
    try:
        with (
            patch.object(Message, "answer", new_callable=AsyncMock) as answer,
            patch.object(
                CallbackQuery,
                "answer",
                new_callable=AsyncMock,
            ),
            patch.object(
                Bot,
                "get_chat",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(type=chat_type),
            ),
            patch.object(
                Bot,
                "get_chat_member",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(status="administrator"),
            ),
            patch.object(Bot, "send_message", new_callable=AsyncMock) as send,
        ):
            await dp.feed_update(bot, Update(update_id=1, message=message))
            assert await selection.report_channel() == -1001234
            assert send.await_args.args[0] == -1001234
            assert send.await_args.kwargs["reply_markup"].inline_keyboard
            answer.assert_not_awaited()  # No denial and no duplicate panel.
            await dp.feed_update(
                bot, Update(update_id=2, message=message.model_copy(update={"text": "Привет"}))
            )
            answer.assert_not_awaited()
            await dp.feed_update(
                bot, Update(update_id=3, message=message.model_copy(update={"text": "/tomorrow"}))
            )
            assert "19.09" in answer.await_args.args[0]
            assert answer.await_args.kwargs["reply_markup"].keyboard
            answer.reset_mock()
            await dp.feed_update(
                bot,
                Update(
                    update_id=4,
                    callback_query=CallbackQuery(
                        id="click",
                        from_user={"id": 30, "is_bot": False, "first_name": "Member"},
                        chat_instance="test",
                        message=message,
                        data="do:hw_next",
                    ),
                ),
            )
            assert "21.09" in answer.await_args.args[0] and "27.09" in answer.await_args.args[0]
            assert answer.await_args.kwargs["reply_markup"].keyboard
    finally:
        await bot.session.close()
        await db.close()
