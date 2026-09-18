"""Explicit environment settings. Secrets are never represented as plain text."""

import os
import ssl
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from pydantic import SecretStr

from app.mesh.auth import USER_AGENT
from app.mesh.exceptions import MeshConfigError


@dataclass(frozen=True)
class Settings:
    auth_file: Path
    database: Path
    timezone: ZoneInfo
    report_time: time
    worker_only: bool
    telegram_token: SecretStr
    allowed_chats: frozenset[int]
    send_empty_daily: bool = False
    profile_id: str | None = None
    timeout: float = 20
    proxy: str | None = None
    ca_bundle: str | None = None
    telegram_proxy: SecretStr | None = None
    report_chat_id: int | None = None

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(".env", override=False)
        token = SecretStr(os.getenv("TELEGRAM_BOT_TOKEN") or "")
        chats = frozenset(
            int(part)
            for part in (os.getenv("TELEGRAM_ALLOWED_CHAT_IDS") or "").replace(",", " ").split()
        )
        worker = (os.getenv("WORKER_ONLY") or "false").lower() == "true"
        report_chat_id = (
            int(os.environ["TELEGRAM_REPORT_CHAT_ID"])
            if os.getenv("TELEGRAM_REPORT_CHAT_ID")
            else None
        )
        if report_chat_id is not None and report_chat_id >= 0:
            raise MeshConfigError("TELEGRAM_REPORT_CHAT_ID должен быть отрицательным ID канала.")
        report_time = time.fromisoformat(os.getenv("DAILY_REPORT_TIME") or "19:00")
        timeout = float(os.getenv("MESH_HTTP_TIMEOUT") or "20")
        if not 0 < timeout <= 120 or report_time.tzinfo or report_time.second:
            raise MeshConfigError("Некорректные timeout или DAILY_REPORT_TIME (HH:MM).")
        if not worker and (not token.get_secret_value() or not chats):
            raise MeshConfigError(
                "Нужны TELEGRAM_BOT_TOKEN и TELEGRAM_ALLOWED_CHAT_IDS; "
                "либо WORKER_ONLY=true для синхронизации без Telegram."
            )
        if any(chat <= 0 for chat in chats):
            raise MeshConfigError("Первая версия поддерживает только личные Telegram chat ID.")
        return cls(
            auth_file=Path(os.getenv("MESH_AUTH_FILE") or ".secrets/auth.json"),
            database=Path(os.getenv("DATABASE_PATH") or "data/bot.db"),
            timezone=ZoneInfo(os.getenv("TZ") or "Europe/Moscow"),
            report_time=report_time,
            worker_only=worker,
            telegram_token=token,
            allowed_chats=chats,
            send_empty_daily=(os.getenv("SEND_EMPTY_DAILY_REPORT") or "false").lower() == "true",
            profile_id=os.getenv("MESH_PROFILE_ID") or None,
            timeout=timeout,
            proxy=os.getenv("MESH_PROXY") or None,
            ca_bundle=os.getenv("MESH_CA_BUNDLE") or None,
            telegram_proxy=SecretStr(os.environ["TELEGRAM_PROXY"])
            if os.getenv("TELEGRAM_PROXY")
            else None,
            report_chat_id=report_chat_id,
        )


def http_client(settings: Settings) -> httpx.AsyncClient:
    tls = ssl.create_default_context(cafile=settings.ca_bundle) if settings.ca_bundle else True
    return httpx.AsyncClient(
        timeout=settings.timeout,
        verify=tls,
        proxy=settings.proxy,
        trust_env=False,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
