from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import CallbackQuery, Message, MessageOriginChannel, Update

from app.bot.ui import dispatcher
from app.mesh.models import Student
from app.repository.database import Database
from app.services.reports import ReportService
from app.services.selection import SelectionService


async def test_connect_channel_publication_buttons_membership_and_channel_commands(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bot.db")
    await db.open()
    await db.save_students(
        [
            Student(id="1", profile_id="p", name="Анна"),
            Student(id="2", profile_id="p", name="Михаил"),
        ]
    )
    now = datetime(2026, 9, 18, 18, tzinfo=ZoneInfo("Europe/Moscow"))
    selection = SelectionService(db, frozenset({10}))
    sync = SimpleNamespace(now=lambda: now, run=AsyncMock(return_value=True))
    settings = SimpleNamespace(allowed_chats=frozenset({10}))
    dp = dispatcher(settings, sync, db, ReportService(db, now.tzinfo), selection)  # type: ignore[arg-type]
    bot = Bot("123456:fake_token_for_test")
    private = Message(
        message_id=1,
        date=now,
        chat={"id": 10, "type": "private"},
        from_user={"id": 10, "is_bot": False, "first_name": "Test"},
        text="/channel -1001234",
    )
    channel = Message(
        message_id=2, date=now, chat={"id": -1001234, "type": "channel"}, text="panel"
    )

    def click(data: str) -> Update:
        return Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="test",
                from_user={"id": 30, "is_bot": False, "first_name": "Subscriber"},
                chat_instance="test",
                message=channel,
                data=data,
            ),
        )

    try:
        with (
            patch.object(Message, "answer", new_callable=AsyncMock) as answer,
            patch.object(
                Message,
                "edit_reply_markup",
                new_callable=AsyncMock,
            ),
            patch.object(CallbackQuery, "answer", new_callable=AsyncMock) as ack,
            patch.object(
                Bot,
                "get_chat",
                new_callable=AsyncMock,
                return_value=SimpleNamespace(type="channel"),
            ),
            patch.object(Bot, "get_chat_member", new_callable=AsyncMock) as membership,
            patch.object(
                Bot,
                "send_message",
                new_callable=AsyncMock,
            ) as send,
        ):
            membership.return_value = SimpleNamespace(status="member", can_post_messages=False)
            await dp.feed_update(bot, Update(update_id=1, message=private))
            assert await selection.report_channel() is None
            send.assert_not_awaited()
            membership.return_value = SimpleNamespace(
                status="administrator", can_post_messages=True
            )
            await dp.feed_update(bot, Update(update_id=1, message=private))
            assert await selection.report_channel() == -1001234
            assert send.await_args.args[0] == -1001234
            actions = {
                button.callback_data
                for row in send.await_args.kwargs["reply_markup"].inline_keyboard
                for button in row
            }
            assert {
                "choose:1",
                "choose:2",
                "choose:all",
                "do:tomorrow",
                "do:hw_week",
                "do:hw_next",
                "do:week",
                "do:month",
            } <= actions
            membership.return_value = SimpleNamespace(status="left")
            await dp.feed_update(bot, click("choose:2"))
            scope = await selection.scope(-1001234)
            assert scope and scope.student_ids == {"1", "2"}
            assert "участникам канала" in ack.await_args.args[0]
            membership.return_value = SimpleNamespace(status="member")
            await dp.feed_update(bot, click("choose:2"))
            scope = await selection.scope(-1001234)
            assert scope and scope.student_ids == {"1", "2"}
            personal = await selection.scope(30, shared_member=True)
            assert personal and personal.student_ids == {"2"}
            answer.reset_mock()
            await dp.feed_update(bot, click("do:tomorrow"))
            content = "".join(call.args[0] for call in answer.await_args_list)
            assert "Михаил" in content and "Анна" not in content and "19.09" in content
            assert answer.await_args.kwargs["reply_markup"].keyboard
            answer.reset_mock()
            post = channel.model_copy(update={"text": "/hw_next"})
            await dp.feed_update(bot, Update(update_id=3, channel_post=post))
            content = "".join(call.args[0] for call in answer.await_args_list)
            assert "21.09" in content and "27.09" in content
            assert "Михаил" in content and "Анна" in content
            answer.reset_mock()
            unbound = post.model_copy(
                update={"chat": post.chat.model_copy(update={"id": -1009999})}
            )
            await dp.feed_update(bot, Update(update_id=4, channel_post=unbound))
            answer.assert_not_awaited()
            # A forwarded post connects a channel already added before the new handler existed.
            membership.return_value = SimpleNamespace(
                status="administrator", can_post_messages=True
            )
            forwarded = private.model_copy(
                update={
                    "text": "post",
                    "forward_origin": MessageOriginChannel(
                        type="channel",
                        date=now,
                        chat={"id": -1007777, "type": "channel"},
                        message_id=3,
                    ),
                }
            )
            await dp.feed_update(bot, Update(update_id=5, message=forwarded))
            assert await selection.report_channel() == -1007777
            assert send.await_args.args[0] == -1007777
            async with db.lock:
                await db.db.execute("DELETE FROM sync_state WHERE key='report_channel'")
                await db.db.commit()
            await dp.feed_update(
                bot, Update(update_id=6, channel_post=channel.model_copy(update={"text": "/start"}))
            )
            assert await selection.report_channel() == -1001234
    finally:
        await bot.session.close()
        await db.close()
