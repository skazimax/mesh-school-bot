"""Safe deployment smoke tests: once/status/backup; no Telegram messages."""

import argparse
import asyncio
import json
import sys
from datetime import datetime

from app.config import Settings, http_client
from app.mesh.exceptions import MeshError
from app.mesh.normalized import MobileMeshClient
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport
from app.repository.database import Database
from app.services.report_images import render_image
from app.services.reports import (
    MonthlyReportService,
    ReportService,
    WeeklyReportService,
    homework_dates,
)
from app.services.selection import SelectionService
from app.services.sync import SyncService


async def run(command: str) -> int:
    settings = Settings.load()
    db = Database(settings.database)
    await db.open()
    try:
        if command == "once":
            async with http_client(settings) as http:
                mesh = MobileMeshClient(
                    Transport(http), AuthStore(settings.auth_file), settings.profile_id
                )
                if not await SyncService(mesh, db, settings.timezone).run():
                    print("Синхронизация не завершена; безопасный статус доступен через status.")
                    return 2
        elif command == "reports":
            reports = ReportService(
                db, settings.timezone, settings.five_threshold, settings.five_weight
            )
            now = datetime.now(settings.timezone)
            daily, new_count = await reports.daily(now.date(), now.isoformat())
            weekly = await WeeklyReportService(reports).render(
                now.date(), now.isoformat(), capture=False
            )
            monthly = await MonthlyReportService(reports).render(now.date(), now.isoformat())
            homework = {}
            for mode in ("tomorrow", "remaining", "next"):
                start, end = homework_dates(now.date(), mode)
                homework[mode] = len(await reports.homework_range(start, end, mode))
            images = []
            if settings.report_format == "image":
                for kind in ("daily", "weekly", "monthly"):
                    for document in await reports.grade_documents(
                        kind, now.date(), now.isoformat()
                    ):
                        image = await asyncio.to_thread(
                            render_image, document, settings.report_font_path
                        )
                        images.append(len(image))
            print(
                json.dumps(
                    {
                        "daily_new_marks": new_count,
                        "report_format": settings.report_format,
                        "image_bytes": images,
                        "report_lengths": {
                            "daily": len(daily),
                            "weekly": len(weekly),
                            "monthly": len(monthly),
                            **homework,
                        },
                    },
                    indent=2,
                )
            )
            return 0
        elif command == "backup":
            name = datetime.now(settings.timezone).strftime("%Y-%m-%d-%H%M%S.db")
            await db.backup(settings.database.parent / "backups" / name)
            print("SQLite backup создан.")
            return 0
        counts = {}
        for table in ("students", "marks", "homework", "subject_snapshots"):
            counts[table] = (await db.rows(f"SELECT COUNT(*) AS n FROM {table}"))[0]["n"]
        print(
            json.dumps(
                {
                    "counts": counts,
                    "last_sync": await db.state("last_sync"),
                    "last_homework_sync": await db.state("last_homework_sync"),
                    "auth": await db.state("auth"),
                    "last_error": await db.state("last_error"),
                    "telegram_configured": bool(settings.telegram_token.get_secret_value())
                    and bool(settings.allowed_chats),
                    "worker_only": settings.worker_only,
                    "report_channel_connected": await SelectionService(
                        db, settings.allowed_chats, settings.report_chat_id
                    ).report_channel()
                    is not None,
                    "automatic_reports_channel_only": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["once", "status", "backup", "reports"])
    command = parser.parse_args().command
    try:
        return asyncio.run(run(command))
    except (MeshError, ValueError, OSError) as exc:
        print(
            str(exc)
            if isinstance(exc, MeshError)
            else f"Ошибка конфигурации ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
