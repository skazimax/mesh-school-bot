"""Mobile mos.ru PKCE authorization, token exchange and activation."""

import asyncio
import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import SecretStr

from app.mesh.exceptions import MeshAPIError, MeshAuthError
from app.mesh.models import AuthState, RegistrationMetadata
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport, auth_error_code

AUTH_BASE = "https://login.mos.ru"
SCHOOL_BASE = "https://school.mos.ru"
PROFILE_INFO_URL = "https://dnevnik.mos.ru/acl/api/users/profile_info"
REDIRECT_URI = "dnevnik-mes://oauth2redirect"
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
)


def solve_hashcash(header: str) -> str:
    try:
        bits = int(header.split(":")[1])
    except (ValueError, IndexError):
        raise MeshAuthError("Неизвестный формат proof-of-work.") from None
    if not header.startswith("1:") or not 1 <= bits <= 20:
        raise MeshAuthError("Неподдерживаемая сложность proof-of-work.")
    # The reference's 15-bit checker also inspects bit 16. A 16-bit solution
    # satisfies both standard hashcash and that implementation.
    effective_bits = 16 if bits == 15 else bits
    for counter in range(10_000_000):
        stamp = header + format(counter, "x")
        digest = hashlib.sha1(stamp.encode(), usedforsecurity=False).digest()
        if int.from_bytes(digest, "big") < 1 << (160 - effective_bits):
            return stamp
    raise MeshAuthError("Не удалось решить proof-of-work за ограниченное число попыток.")


def token_expiry(token: str) -> int | None:
    """Unverified JWT claim for diagnostics only, never an authorization decision."""
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        expiry = payload.get("exp")
        return expiry if type(expiry) is int else None
    except (ValueError, IndexError, AttributeError):
        return None


class ResearchAuth:
    def __init__(self, transport: Transport, store: AuthStore) -> None:
        self.transport = transport
        self.store = store

    async def activate(self, token: str) -> list[dict[str, Any]]:
        profiles = await self.transport.json(
            "GET",
            PROFILE_INFO_URL,
            headers={"Auth-Token": token, "partner-source-id": "MOBILE"},
        )
        if (
            not isinstance(profiles, list)
            or not profiles
            or not all(isinstance(profile, dict) and profile.get("id") for profile in profiles)
        ):
            raise MeshAuthError("Активация токена не вернула доступные профили.")
        return profiles

    async def exchange(self, state: AuthState, mos: Any, *, via: str) -> AuthState:
        if not isinstance(mos, dict) or not isinstance(mos.get("access_token"), str):
            raise MeshAuthError("mos.ru не вернул access_token.")
        # Persist rotation BEFORE exchange/activation, even if those steps fail.
        # An older refresh token may already have been invalidated by mos.ru.
        if isinstance(mos.get("refresh_token"), str) and mos["refresh_token"]:
            state.refresh_token = SecretStr(mos["refresh_token"])
            self.store.save(state)
        mesh = await self.transport.json(
            "POST",
            SCHOOL_BASE + "/v3/auth/sudir/auth",
            json={
                "user_authentication_for_mobile_request": {
                    "mos_access_token": mos["access_token"],
                }
            },
        )
        try:
            token = mesh["user_authentication_for_mobile_response"]["mesh_access_token"]
        except (KeyError, TypeError):
            raise MeshAuthError("МЭШ не вернул mesh_access_token.") from None
        if not isinstance(token, str) or not token:
            raise MeshAuthError("МЭШ вернул пустой mesh_access_token.")
        await self.activate(token)
        # Do not overwrite a previously active mesh token until activation succeeds.
        state.mesh_access_token = SecretStr(token)
        state.mesh_expires_at = token_expiry(token)
        state.mos_expires_in = mos.get("expires_in") if type(mos.get("expires_in")) is int else None
        state.refreshed_at = datetime.now(UTC).isoformat()
        state.refresh_via = via
        state.activated = True
        self.store.save(state)
        return state

    async def refresh(self, *, grant: str = "auto") -> AuthState:
        state = self.store.load()
        credentials = httpx.BasicAuth(
            state.client_id.get_secret_value(), state.client_secret.get_secret_value()
        )
        if grant in {"auto", "client_credentials"}:
            try:
                mos = await self.transport.json(
                    "POST",
                    AUTH_BASE + "/sps/oauth/te",
                    auth=credentials,
                    data={"grant_type": "client_credentials", "scope": "openid profile"},
                )
            except MeshAuthError as exc:
                # Network/server failures are not evidence to consume a rotating token.
                if (
                    grant != "auto"
                    or exc.code
                    not in {
                        "access_denied",
                        "invalid_grant",
                        "unsupported_grant_type",
                        "unauthorized_client",
                    }
                    or not state.refresh_token
                ):
                    raise
            else:
                return await self.exchange(state, mos, via="client_credentials")
        if not state.refresh_token:
            raise MeshAuthError("Нет refresh_token. Нужна первичная авторизация.")
        mos = await self.transport.json(
            "POST",
            AUTH_BASE + "/sps/oauth/te",
            auth=credentials,
            data={
                "grant_type": "refresh_token",
                "refresh_token": state.refresh_token.get_secret_value(),
            },
        )
        return await self.exchange(state, mos, via="refresh_token")

    async def login(
        self,
        metadata: RegistrationMetadata,
        username: str,
        password: str,
        ask_otp: Callable[[], str],
    ) -> AuthState:
        registration = await self.transport.json(
            "POST",
            AUTH_BASE + "/sps/oauth/register",
            headers={"Authorization": "Bearer " + metadata.bearer.get_secret_value()},
            json={
                "software_id": "dnevnik.mos.ru",
                "device_type": "android_phone",
                "software_statement": metadata.software_statement.get_secret_value(),
            },
        )
        if not isinstance(registration, dict) or not all(
            isinstance(registration.get(key), str) and registration[key]
            for key in ("client_id", "client_secret")
        ):
            raise MeshAuthError("Регистрация устройства не вернула client credentials.")
        state = AuthState(
            client_id=SecretStr(registration["client_id"]),
            client_secret=SecretStr(registration["client_secret"]),
        )
        verifier = secrets.token_urlsafe(60)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
        initial = await self.transport.json(
            "GET",
            AUTH_BASE + "/sps/oauth/ae",
            params={
                "response_type": "code",
                "scope": (
                    "birthday contacts openid profile snils blitz_change_password "
                    "blitz_user_rights blitz_qr_auth"
                ),
                "access_type": "offline",
                "display": "script",
                "client_id": state.client_id.get_secret_value(),
                "bip_action_hint": "used_sms",
                "code_challenge": challenge.rstrip("="),
                "code_challenge_method": "S256",
                "redirect_uri": REDIRECT_URI,
            },
        )
        if not isinstance(initial, dict) or not isinstance(initial.get("items"), list):
            raise MeshAuthError("Неизвестный ответ инициализации OAuth.")
        password_item = next(
            (
                item
                for item in initial["items"]
                if isinstance(item, dict) and item.get("inquire") == "login_with_password"
            ),
            None,
        )
        if not password_item:
            raise MeshAuthError("OAuth не предложил поддерживаемый вход по паролю.")
        proof = password_item.get("proofOfWork")
        stamp = await asyncio.to_thread(solve_hashcash, str(proof)) if proof else ""
        response = await self.transport.request(
            "POST",
            AUTH_BASE + "/sps/login/methods/headless/password",
            json={"login": username, "password": password, "proofOfWork": stamp},
        )
        code = await self._authorization_code(response, ask_otp)
        mos = await self.transport.json(
            "POST",
            AUTH_BASE + "/sps/oauth/te",
            auth=httpx.BasicAuth(
                state.client_id.get_secret_value(), state.client_secret.get_secret_value()
            ),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        return await self.exchange(state, mos, via="authorization_code")

    async def _authorization_code(
        self,
        response: httpx.Response,
        ask_otp: Callable[[], str],
    ) -> str:
        sms_requested = False
        otp_submitted = False
        trust_requested = False
        for _ in range(12):
            location = response.headers.get("location", "")
            parsed = urlsplit(location)
            # Never follow arbitrary redirects with credentials or cookies.
            if location and (parsed.scheme != "dnevnik-mes" or parsed.netloc != "oauth2redirect"):
                raise MeshAuthError("Неизвестный redirect авторизации; нужна проверка flow.")
            codes = parse_qs(parsed.query).get("code")
            try:
                data = response.json()
            except ValueError:
                data = {}
            error = auth_error_code(data)
            if error:
                raise MeshAuthError(f"Вход mos.ru: {error}", code=error)
            if response.status_code >= 400:
                raise MeshAPIError(f"HTTP {response.status_code}; endpoint={response.url.path}")
            if codes and codes[0]:
                return str(codes[0])
            if not isinstance(data, dict):
                raise MeshAuthError("Неизвестный формат ответа авторизации.")
            if isinstance(data.get("trust_code"), str) and data["trust_code"]:
                return str(data["trust_code"])
            if data.get("inquire") == "enter_sms_code" and not otp_submitted:
                otp = ask_otp().strip()
                if not otp or not otp.isdigit():
                    raise MeshAuthError("SMS-код должен содержать цифры.")
                otp_submitted = True
                response = await self.transport.request(
                    "POST",
                    AUTH_BASE + "/sps/login/methods/headless/sms/bind",
                    data={"sms-code": otp},
                )
                continue
            items = data.get("items", [])
            if (
                isinstance(items, list)
                and any(
                    isinstance(item, dict)
                    and item.get("inquire") in {"ask_to_send_sms", "go_to_web"}
                    for item in items
                )
                and not sms_requested
            ):
                sms_requested = True
                response = await self.transport.request(
                    "POST",
                    AUTH_BASE + "/sps/login/methods/headless/sms/bind",
                    content="",
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                continue
            if otp_submitted and "Доверять" in response.text and not trust_requested:
                trust_requested = True
                response = await self.transport.request(
                    "GET",
                    AUTH_BASE + "/sps/login/ur/askToTrust",
                )
                continue
            raise MeshAuthError("Неподдерживаемый шаг входа (например, CAPTCHA/согласие).")
        raise MeshAuthError("Превышено ограничение числа шагов авторизации.")
