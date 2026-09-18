"""Single scheduler task; persistent report progress and throttled recovery notices."""

import asyncio
import html
import json
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile

from app.bot.ui import channel_controls
from app.config import Settings
from app.repository.database import Database
from app.services.report_images import IMAGE_PREFIX, GradeDocument, render_image
from app.services.reports import (
    MonthlyReportService,
    ReportService,
    WeeklyReportService,
    day_start,
    split_messages,
)
from app.services.selection import SelectionService
from app.services.sync import SyncService

logger = logging.getLogger(__name__)


def elapsed(previous: str | None, now: datetime, seconds: int) -> bool:
    if not previous:
        return True
    return (now - datetime.fromisoformat(previous)).total_seconds() >= seconds


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        sync: SyncService,
        reports: ReportService,
        db: Database,
        bot: Bot | None = None,
        selection: SelectionService | None = None,
    ) -> None:
        self.settings, self.sync, self.reports, self.db, self.bot = settings, sync, reports, db, bot
        self.selection = selection

    async def completed(self, key: str) -> bool:
        rows = await self.db.rows("SELECT complete FROM report_delivery WHERE key=?", (key,))
        return bool(rows and rows[0]["complete"])

    async def send(
        self,
        key: str,
        chat: int,
        text: str,
        now: str,
        student_ids: frozenset[str] | None = None,
        documents: list[GradeDocument] | None = None,
    ) -> None:
        if self.bot is None:
            return
        frozen = (
            [document.freeze() for document in documents]
            if documents is not None
            else split_messages(text)
        )
        delivery = await self.db.prepare_delivery(key, frozen, now)
        if delivery["complete"]:
            return
        parts = json.loads(delivery["content"])
        for index in range(delivery["next_part"], len(parts)):
            image = None
            if parts[index].startswith(IMAGE_PREFIX):
                image = await asyncio.to_thread(
                    render_image,
                    GradeDocument.thaw(parts[index]),
                    getattr(self.settings, "report_font_path", None),
                )
            part = "<pre>" + html.escape(parts[index]) + "</pre>"
            markup = None
            if self.selection:
                scope = await self.selection.scope(chat)
                if not scope or student_ids is not None and not student_ids <= scope.student_ids:
                    raise PermissionError("report scope changed")
                if chat != await self.selection.report_channel():
                    raise PermissionError("report channel changed")
                markup = channel_controls(scope)
                if image is not None:
                    await self.bot.send_photo(
                        chat, BufferedInputFile(image, filename="grades.png"), reply_markup=markup
                    )
                else:
                    await self.bot.send_message(chat, part, parse_mode="HTML", reply_markup=markup)
            else:
                if image is not None:
                    await self.bot.send_photo(chat, BufferedInputFile(image, filename="grades.png"))
                else:
                    await self.bot.send_message(chat, part, parse_mode="HTML")
            await self.db.delivery_progress(key, index + 1, index + 1 == len(parts), now)
        logger.info("report.sent type=%s", key.split(":")[0])

    async def report_jobs(self, now: datetime) -> None:
        if (
            self.bot is None
            or now.weekday() >= 5
            or now.time() < self.settings.report_time
            or now.hour >= 22
        ):
            return
        day = now.date().isoformat()
        pending: list[tuple[int, str, frozenset[str] | None]] = []
        if self.selection:
            channel = await self.selection.report_channel()
            if channel is None:
                return  # Explicitly wait for a channel; never fall back to private auto-deliveries.
            recipients = [channel]
        else:
            recipients = list(self.settings.allowed_chats)
        for chat in recipients:
            scope = await self.selection.scope(chat) if self.selection else None
            if self.selection and not scope:
                continue
            # Preserve existing all-family administrator deliveries and cutoffs.
            suffix = "" if not scope or scope.selected == scope.students else ":" + scope.key
            ids = scope.student_ids if scope else None
            daily_key = f"daily:{day}:{chat}{suffix}"
            weekly_key = f"weekly:{day}:{chat}{suffix}"
            if (
                not await self.completed(daily_key)
                or now.weekday() == 4
                and not await self.completed(weekly_key)
            ):
                pending.append((chat, suffix, ids))
        if not pending or not elapsed(await self.db.state("report_attempt:" + day), now, 900):
            return
        await self.db.set_state("report_attempt:" + day, now.isoformat(), now.isoformat())
        if not await self.sync.run():
            return  # Do not send incomplete automatic reports during provider failures.
        cutoff = self.sync.now().isoformat()
        for chat, suffix, ids in pending:
            try:
                daily_key = f"daily:{day}:{chat}{suffix}"
                if not await self.completed(daily_key):
                    since = await self.db.state(f"daily_cutoff:{chat}{suffix}") or day_start(
                        now.date(), self.settings.timezone
                    )
                    text, count = await self.reports.daily(now.date(), cutoff, since, ids)
                    frozen = await self.db.rows(
                        "SELECT key FROM report_delivery WHERE key=?", (daily_key,)
                    )
                    if count or self.settings.send_empty_daily or frozen:
                        documents = (
                            await self.reports.grade_documents(
                                "daily", now.date(), cutoff, ids, since=since
                            )
                            if getattr(self.settings, "report_format", "text") == "image"
                            else None
                        )
                        await self.send(daily_key, chat, text, cutoff, ids, documents)
                    else:
                        await self.db.prepare_delivery(daily_key, [], cutoff)
                        await self.db.delivery_progress(daily_key, 0, True, cutoff)
                    # The delivery's frozen cutoff may predate a restart/retry.
                    saved = (
                        await self.db.rows(
                            "SELECT created_at FROM report_delivery WHERE key=?", (daily_key,)
                        )
                    )[0]
                    await self.db.set_state(
                        f"daily_cutoff:{chat}{suffix}", saved["created_at"], cutoff
                    )
                if now.weekday() == 4:
                    weekly_key = f"weekly:{day}:{chat}{suffix}"
                    weekly = await WeeklyReportService(self.reports).render(
                        now.date(), cutoff, ids, capture=False
                    )
                    monthly = await MonthlyReportService(self.reports).render(
                        now.date(), cutoff, ids
                    )
                    documents = None
                    if getattr(self.settings, "report_format", "text") == "image":
                        documents = await self.reports.grade_documents(
                            "weekly", now.date(), cutoff, ids
                        )
                        documents += await self.reports.grade_documents(
                            "monthly", now.date(), cutoff, ids
                        )
                    await self.send(
                        weekly_key, chat, weekly + "\n\n" + monthly, cutoff, ids, documents
                    )
                    saved = (
                        await self.db.rows(
                            "SELECT created_at FROM report_delivery WHERE key=?", (weekly_key,)
                        )
                    )[0]
                    await self.reports.capture_weekly(now.date(), saved["created_at"], ids)
            except Exception as exc:
                logger.warning("report.send failed error=%s", type(exc).__name__)

    async def auth_notice(self, now: datetime) -> None:
        if self.bot is None or await self.db.state("auth") != "required":
            return
        for chat in self.settings.allowed_chats:
            key = f"auth_notice:{chat}"
            if elapsed(await self.db.state(key), now, 86400):
                # Throttle before send too, so a failing Telegram call cannot spin.
                await self.db.set_state(key, now.isoformat(), now.isoformat())
                try:
                    await self.bot.send_message(
                        chat, "⚠️ Авторизация МЭШ истекла. Требуется повторный интерактивный вход."
                    )
                except Exception as exc:
                    logger.warning("auth.notice failed error=%s", type(exc).__name__)

    async def tick(self) -> None:
        now = self.sync.now()
        if 7 <= now.hour < 21 and elapsed(await self.db.state("last_sync_attempt"), now, 1800):
            await self.db.set_state("last_sync_attempt", now.isoformat(), now.isoformat())
            await self.sync.run()
        await self.report_jobs(now)
        await self.auth_notice(now)
        day = now.date().isoformat()
        if await self.db.state("last_backup_day") != day:
            await self.db.backup(self.settings.database.parent / "backups" / f"{day}.db")
            await self.db.set_state("last_backup_day", day, now.isoformat())
            # Keep the latest fourteen daily backups; no deletion outside the dedicated directory.
            directory = self.settings.database.parent / "backups"
            old = sorted(directory.glob("????-??-??.db"))[:-14]
            for path in old:
                await asyncio.to_thread(path.unlink)
            logger.info("database.backup completed")

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:
                logger.warning("scheduler.tick failed error=%s", type(exc).__name__)
            await asyncio.sleep(20)
