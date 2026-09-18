"""systemd entry point with graceful shutdown and an explicit sync-only mode."""

import asyncio
import contextlib
import fcntl
import logging
import os
import signal

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from app.bot.ui import BOT_COMMANDS, dispatcher
from app.config import Settings, http_client
from app.jobs.scheduler import Scheduler
from app.mesh.normalized import MobileMeshClient
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport
from app.repository.database import Database
from app.services.reports import ReportService
from app.services.selection import SelectionService
from app.services.sync import SyncService

logger = logging.getLogger(__name__)


async def run() -> None:
    os.umask(0o077)
    settings = Settings.load()
    settings.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (settings.database.parent / "service.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = Database(settings.database)
        await db.open()
        selection = SelectionService(db, settings.allowed_chats, settings.report_chat_id)
        bot = (
            None
            if settings.worker_only
            else Bot(
                settings.telegram_token.get_secret_value(),
                session=AiohttpSession(
                    timeout=20,
                    proxy=settings.telegram_proxy.get_secret_value()
                    if settings.telegram_proxy
                    else None,
                ),
            )
        )
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stopped.set)
        tasks: list[asyncio.Task[object]] = []
        try:
            async with http_client(settings) as http:
                mesh = MobileMeshClient(
                    Transport(http), AuthStore(settings.auth_file), settings.profile_id
                )
                sync = SyncService(mesh, db, settings.timezone)
                reports = ReportService(db, settings.timezone)
                await sync.run()
                now = sync.now().isoformat()
                await db.set_state("last_sync_attempt", now, now)
                scheduler = Scheduler(settings, sync, reports, db, bot, selection)
                tasks.append(asyncio.create_task(scheduler.run()))
                if bot:
                    await bot.get_me()  # Verify credentials, without sending a test message.
                    try:
                        await bot.set_my_commands(BOT_COMMANDS)
                    except Exception as exc:
                        logger.warning("bot.menu failed error=%s", type(exc).__name__)
                    tasks.append(
                        asyncio.create_task(
                            dispatcher(settings, sync, db, reports, selection).start_polling(
                                bot, handle_signals=False
                            )
                        )
                    )
                logger.info("app.started mode=%s", "worker" if settings.worker_only else "telegram")
                waiter = asyncio.create_task(stopped.wait())
                tasks.append(waiter)
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    if task.done() and not task.cancelled():
                        task.result()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if bot:
                await bot.session.close()
            await db.close()
            logger.info("app.stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for name in ("httpx", "httpcore", "aiogram"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(run())
    except Exception as exc:
        logger.error("app.failed error=%s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
