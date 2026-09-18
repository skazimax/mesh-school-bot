"""Bounded read retries. OAuth/OTP POSTs are never automatically replayed."""

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.mesh.exceptions import MeshAPIError, MeshAuthError

logger = logging.getLogger(__name__)
SAFE_AUTH_CODES = {
    "invalid_client",
    "invalid_grant",
    "access_denied",
    "invalid_credentials",
    "pswd_method_temp_locked",
    "invalid_otp",
    "expired",
    "no_attempts",
    "no_subject_found",
    "unauthorized_client",
    "unsupported_grant_type",
}


def auth_error_code(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    code = data.get("error")
    errors = data.get("errors")
    if not code and isinstance(errors, list) and errors and isinstance(errors[0], dict):
        code = errors[0].get("code")
    if not code:
        return None
    return code if isinstance(code, str) and code in SAFE_AUTH_CODES else "unknown_auth_error"


class Transport:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        endpoint = urlsplit(url).path  # no query, user IDs, redirect codes or credentials
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                if attempt + 1 == attempts:
                    raise MeshAPIError(
                        f"Сетевая ошибка {type(exc).__name__}; endpoint={endpoint}. "
                        "Проверьте сеть, прокси и доверенные TLS-сертификаты."
                    ) from None
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < attempts:
                logger.warning(
                    "mesh.api retry status=%s endpoint=%s", response.status_code, endpoint
                )
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            return response
        raise AssertionError("unreachable")

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        return self.decode(await self.request(method, url, **kwargs))

    @staticmethod
    def decode(response: httpx.Response) -> Any:
        endpoint = response.url.path
        try:
            data = response.json()
        except ValueError:
            data = None
        code = auth_error_code(data)
        if code or response.status_code == 401:
            raise MeshAuthError(
                f"Авторизация: {code or 'unauthorized'}; endpoint={endpoint}", code=code
            )
        if not response.is_success:
            raise MeshAPIError(f"HTTP {response.status_code}; endpoint={endpoint}")
        if data is None:
            raise MeshAPIError(f"Ожидался JSON; endpoint={endpoint}")
        return data
