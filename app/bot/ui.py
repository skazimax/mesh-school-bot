"""Shared family access, per-chat child selection, and connected-channel controls."""

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    MessageOriginChannel,
    ReplyKeyboardMarkup,
)

from app.config import Settings
from app.repository.database import Database
from app.services.reports import (
    MonthlyReportService,
    ReportService,
    WeeklyReportService,
    homework_dates,
    telegram_parts,
)
from app.services.selection import SelectionService, StudentSelection
from app.services.sync import SyncService

logger = logging.getLogger(__name__)
BOT_COMMANDS = [
    BotCommand(command=command, description=description)
    for command, description in (
        ("start", "Меню и помощь"),
        ("children", "Выбрать ребёнка"),
        ("tomorrow", "ДЗ на завтра"),
        ("hw_week", "ДЗ до конца недели"),
        ("hw_next", "ДЗ на следующую неделю"),
        ("week", "Оценки за неделю"),
        ("month", "Оценки за месяц"),
        ("channel", "Подключить канал отчётов"),
        ("status", "Состояние бота"),
    )
]
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📚 Завтра"), KeyboardButton(text="📚 До конца недели")],
        [KeyboardButton(text="📚 Следующая неделя")],
        [KeyboardButton(text="📊 Оценки за неделю"), KeyboardButton(text="📈 Оценки за месяц")],
        [KeyboardButton(text="👤 Выбрать ребёнка")],
    ],
    resize_keyboard=True,
)


def authorized(message: Message, allowed: frozenset[int]) -> bool:
    return (
        message.chat.type == "private"
        and message.chat.id in allowed
        and message.from_user is not None
        and message.from_user.id == message.chat.id
    )


def child_buttons(scope: StudentSelection) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✓ " if len(scope.selected) == 1 and s.id in scope.student_ids else "")
                + s.name,
                callback_data=f"choose:{s.id}",
            )
        ]
        for s in scope.students
    ]
    if len(scope.students) > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✓ " if scope.selected == scope.students else "") + "Все дети",
                    callback_data="choose:all",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def channel_controls(scope: StudentSelection) -> InlineKeyboardMarkup:
    rows = child_buttons(scope).inline_keyboard + [
        [
            InlineKeyboardButton(text="📚 Завтра", callback_data="do:tomorrow"),
            InlineKeyboardButton(text="📚 До конца недели", callback_data="do:hw_week"),
        ],
        [InlineKeyboardButton(text="📚 Следующая неделя", callback_data="do:hw_next")],
        [
            InlineKeyboardButton(text="📊 Неделя", callback_data="do:week"),
            InlineKeyboardButton(text="📈 Месяц", callback_data="do:month"),
        ],
        [InlineKeyboardButton(text="⚙️ Статус", callback_data="do:status")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dispatcher(
    settings: Settings,
    sync: SyncService,
    db: Database,
    reports: ReportService,
    selection: SelectionService,
) -> Dispatcher:
    router = Router()

    async def post_panel(bot: Bot, chat: int) -> None:
        scope = await selection.scope(chat)
        if scope:
            await bot.send_message(
                chat,
                "📚 Дневник МЭШ\nВыберите ребёнка и нужный отчёт.",
                reply_markup=channel_controls(scope),
            )

    async def connect(bot: Bot, channel: int) -> None:
        info = await bot.get_chat(channel)
        member = await bot.get_chat_member(channel, bot.id)
        if (
            info.type not in {"channel", "group", "supergroup"}
            or member.status != "administrator"
            or info.type == "channel"
            and not getattr(member, "can_post_messages", False)
        ):
            raise ValueError("channel posting rights required")
        await selection.bind_channel(channel, sync.now())
        await post_panel(bot, channel)

    async def family_admin(bot: Bot, chat: int) -> bool:
        for user in settings.allowed_chats:
            try:
                member = await bot.get_chat_member(chat, user)
                if member.status in {"creator", "administrator"}:
                    return True
            except Exception as exc:
                logger.warning("bot.chat_admin failed error=%s", type(exc).__name__)
        return False

    async def group_actor(message: Message, bot: Bot) -> bool:
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True  # Telegram's anonymous administrator, inside the connected chat.
        if not message.from_user or message.from_user.is_bot:
            return False
        if message.from_user.id in settings.allowed_chats:
            return True
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in {"creator", "administrator", "member"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )

    @router.my_chat_member()
    async def membership(event: ChatMemberUpdated, bot: Bot) -> None:
        if (
            event.chat.type not in {"channel", "group", "supergroup"}
            or event.from_user.id not in settings.allowed_chats
        ):
            return
        member = event.new_chat_member
        if member.status == "administrator" and (
            event.chat.type != "channel" or getattr(member, "can_post_messages", False)
        ):
            try:
                await connect(bot, event.chat.id)
                logger.info("bot.channel connected")
            except Exception as exc:
                logger.warning("bot.channel failed error=%s", type(exc).__name__)

    async def process(message: Message, text: str) -> None:
        scope = await selection.scope(message.chat.id)
        if not scope:
            await message.answer("Данные семьи пока недоступны. Попробуйте позже.")
            return
        in_channel = message.chat.type in {"channel", "group", "supergroup"}
        today = sync.now().date()
        if text == "/start":
            if in_channel:
                answer = "📚 Дневник МЭШ\nВыберите ребёнка и нужный отчёт."
            else:
                connected = await selection.report_channel() is not None
                answer = (
                    "📚 Дневник МЭШ\n\n/children — выбор ребёнка\n"
                    "/tomorrow — ДЗ на завтра\n/hw_week — ДЗ до конца недели\n"
                    "/hw_next — ДЗ на следующую неделю\n"
                    "/week — оценки за неделю\n/month — оценки за месяц\n/status — состояние\n\n"
                    "Среднее — МЭШ за триместр.\n↑ ↓ → — динамика к прошлой неделе.\n"
                    "— — нет среднего или сравнения.\n\n"
                    "Выбрано: "
                    + ", ".join(s.name for s in scope.selected)
                    + (
                        "\nАвтоотчёты: в подключённый канал."
                        if connected
                        else "\nАвтоотчёты ждут подключения канала. Добавьте бота администратором "
                        "с правом публикации. /channel — подключение."
                    )
                )
        elif text in {"/children", "👤 Выбрать ребёнка"}:
            await message.answer(
                "Выберите ребёнка. В канале выбор действует и для автоотчётов.",
                reply_markup=channel_controls(scope) if in_channel else child_buttons(scope),
            )
            return
        elif text in {
            "/tomorrow",
            "📚 Завтра",
            "/hw_week",
            "📚 До конца недели",
            "/hw_next",
            "📚 Следующая неделя",
        }:
            mode = (
                "tomorrow"
                if text in {"/tomorrow", "📚 Завтра"}
                else "remaining"
                if text in {"/hw_week", "📚 До конца недели"}
                else "next"
            )
            start, end = homework_dates(today, mode)
            updated = await sync.run(homework_only=True)
            title = {
                "tomorrow": "ДЗ на завтра",
                "remaining": "ДЗ до конца недели",
                "next": "ДЗ на следующую неделю",
            }[mode]
            answer = await reports.homework_range(
                start, end, title, stale=not updated, student_ids=scope.student_ids
            )
        elif text in {
            "/week",
            "📊 Неделя",
            "📊 Оценки за неделю",
            "/month",
            "📈 Месяц",
            "📈 Оценки за месяц",
        }:
            updated = await sync.run()
            service = (
                WeeklyReportService(reports)
                if text in {"/week", "📊 Неделя", "📊 Оценки за неделю"}
                else MonthlyReportService(reports)
            )
            answer = "⚠️ Сохранённые данные: МЭШ недоступен.\n\n" if not updated else ""
            if isinstance(service, WeeklyReportService):
                answer += await service.render(
                    today, sync.now().isoformat(), scope.student_ids, capture=updated
                )
            else:
                answer += await service.render(today, sync.now().isoformat(), scope.student_ids)
        elif text == "/status":
            state = await db.state("auth")
            channel = await selection.report_channel()
            answer = (
                f"✅ Бот работает\nМЭШ: {'✅' if state == 'ok' else '⚠️'}\n"
                f"Оценки: {await db.state('last_sync') or 'не синхронизированы'}\n"
                f"ДЗ: {await db.state('last_homework_sync') or 'не синхронизированы'}\n"
                f"Авторизация: {'нужен повторный вход' if state == 'required' else state}\n"
                f"Канал автоотчётов: {'подключён' if channel else 'не подключён'}\n"
                "Выбрано: " + ", ".join(s.name for s in scope.selected)
            )
        elif text in {"/today", "📚 Сегодня"}:
            answer = (
                "ДЗ на сегодня убраны. Выберите «Завтра», «До конца недели» или «Следующая неделя»."
            )
        else:
            answer = "Выберите команду на клавиатуре или /start."
        for part in telegram_parts(answer):
            current = await selection.scope(message.chat.id)
            if not current or current.student_ids != scope.student_ids:
                return
            if in_channel and message.chat.id != await selection.report_channel():
                return
            await message.answer(
                part,
                parse_mode="HTML",
                reply_markup=channel_controls(current) if in_channel else KEYBOARD,
            )

    @router.callback_query()
    async def callback(query: CallbackQuery, bot: Bot) -> None:
        message = query.message
        if not isinstance(message, Message):
            await query.answer("Доступ запрещён.", show_alert=True)
            return
        in_channel = (
            message.chat.type in {"channel", "group", "supergroup"}
            and message.chat.id == await selection.report_channel()
        )
        in_private = (
            message.chat.type == "private"
            and message.chat.id == query.from_user.id
            and query.from_user.id in settings.allowed_chats
        )
        if not in_private and not in_channel:
            await query.answer("Доступ запрещён.", show_alert=True)
            return
        try:
            if in_channel and query.from_user.id not in settings.allowed_chats:
                member = await bot.get_chat_member(message.chat.id, query.from_user.id)
                if member.status not in {"creator", "administrator", "member"} and not (
                    member.status == "restricted" and getattr(member, "is_member", False)
                ):
                    await query.answer("Управление доступно участникам канала.", show_alert=True)
                    return
            action, _, value = (query.data or "").partition(":")
            if action == "choose":
                if not await selection.select(
                    message.chat.id, None if value == "all" else value, sync.now()
                ):
                    await query.answer("Ребёнок не найден.", show_alert=True)
                    return
                scope = await selection.scope(message.chat.id)
                await query.answer("Выбор сохранён.")
                if scope:
                    markup = channel_controls(scope) if in_channel else child_buttons(scope)
                    if message.reply_markup != markup:
                        await message.edit_reply_markup(reply_markup=markup)
                    if in_private:
                        await message.answer(
                            "Выбрано: " + ", ".join(s.name for s in scope.selected),
                            reply_markup=KEYBOARD,
                        )
            elif action == "do" and value in {
                "tomorrow",
                "hw_week",
                "hw_next",
                "week",
                "month",
                "status",
            }:
                await query.answer("Готовлю…")
                await process(message, "/" + value)
            else:
                await query.answer()
        except Exception as exc:
            logger.warning("bot.callback failed error=%s", type(exc).__name__)
            await message.answer("Не удалось выполнить действие. Попробуйте позже.")

    @router.message()
    async def command(message: Message, bot: Bot) -> None:
        if message.chat.type in {"group", "supergroup"}:
            raw = (message.text or "").strip()
            if not raw.startswith("/"):
                return  # Never respond to ordinary group conversation.
            name = raw.split(maxsplit=1)[0].split("@", 1)[0]
            if name not in {
                "/start",
                "/children",
                "/tomorrow",
                "/hw_week",
                "/hw_next",
                "/week",
                "/month",
                "/status",
                "/today",
            }:
                return
            try:
                target = await selection.report_channel()
                known_sender = bool(
                    message.from_user and message.from_user.id in settings.allowed_chats
                )
                if (
                    target is None
                    and name == "/start"
                    and (known_sender or await family_admin(bot, message.chat.id))
                ):
                    await connect(bot, message.chat.id)
                    logger.info("bot.chat connected type=%s", message.chat.type)
                    return  # connect already published the control panel.
                if target == message.chat.id and await group_actor(message, bot):
                    await process(message, name)
            except Exception as exc:
                logger.warning("bot.group_command failed error=%s", type(exc).__name__)
                await message.answer("Не удалось выполнить команду. Проверьте права бота.")
            return
        if not authorized(message, settings.allowed_chats):
            await message.answer("Доступ запрещён.")
            return
        raw = (message.text or "").strip()
        words = raw.split(maxsplit=1)
        text = words[0].split("@", 1)[0] if raw.startswith("/") and words else raw
        try:
            if isinstance(message.forward_origin, MessageOriginChannel):
                await connect(bot, message.forward_origin.chat.id)
                await message.answer("Канал подключён. В нём опубликованы кнопки управления.")
                return
            if text == "/channel":
                if len(words) == 2:
                    await connect(bot, int(words[1]))
                    await message.answer("Канал подключён. Автоотчёты будут только в нём.")
                else:
                    channel = await selection.report_channel()
                    await message.answer(
                        (
                            f"Канал подключён: {channel}.\n"
                            if channel
                            else "Канал ещё не подключён.\n"
                        )
                        + "Добавьте бота администратором канала с правом публикации — "
                        "он подключится автоматически. Если уже добавлен, "
                        "перешлите мне любой пост из канала или отправьте /channel -100…",
                    )
                return
            await process(message, text)
        except Exception as exc:
            logger.warning("bot.command failed error=%s", type(exc).__name__)
            await message.answer(
                "Не удалось выполнить команду. Проверьте настройки и попробуйте позже."
            )

    @router.channel_post()
    async def channel_post(message: Message, bot: Bot) -> None:
        if await selection.report_channel() is None:
            # Recover an addition that happened before my_chat_member updates were enabled.
            # A known family user must administer this channel.
            # An unknown channel cannot bind itself.
            try:
                for user in settings.allowed_chats:
                    member = await bot.get_chat_member(message.chat.id, user)
                    if member.status in {"creator", "administrator"}:
                        await connect(bot, message.chat.id)
                        logger.info("bot.channel connected from_post")
                        break
            except Exception as exc:
                logger.warning("bot.channel_discovery failed error=%s", type(exc).__name__)
        if message.chat.id != await selection.report_channel() or not (
            message.text or ""
        ).startswith("/"):
            return
        if message.from_user and message.from_user.is_bot:
            return
        try:
            command = (message.text or "").split(maxsplit=1)[0].split("@", 1)[0]
            await process(message, command)
        except Exception as exc:
            logger.warning("bot.channel_command failed error=%s", type(exc).__name__)
            await message.answer("Не удалось выполнить команду. Попробуйте позже.")

    result = Dispatcher()
    result.include_router(router)
    return result
