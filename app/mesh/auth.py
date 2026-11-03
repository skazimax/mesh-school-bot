"""Mobile mos.ru PKCE authorization, token exchange and activation."""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import SplitResult, parse_qs, urlsplit

import httpx
from pydantic import SecretStr

from app.mesh.exceptions import MeshAPIError, MeshAuthError
from app.mesh.models import AuthState, RegistrationMetadata
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport, auth_error_code

logger = logging.getLogger(__name__)

AUTH_BASE = "https://login.mos.ru"
SCHOOL_BASE = "https://school.mos.ru"
PROFILE_INFO_URL = "https://dnevnik.mos.ru/acl/api/users/profile_info"
REDIRECT_URI = "dnevnik-mes://oauth2redirect"
# The school.mos.ru website's own OAuth client: no device registration needed.
WEB_REDIRECT_URI = SCHOOL_BASE + "/v3/auth/sudir/callback"
WEB_AUTHORIZATION = {
    "response_type": "code",
    "access_type": "offline",
    "client_id": "dnevnik.mos.ru",
    "scope": (
        "openid profile birthday contacts snils blitz_user_rights "
        "blitz_change_password offline_access"
    ),
    "redirect_uri": WEB_REDIRECT_URI,
}
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


class PageForms(HTMLParser):
    """Forms of a mos.ru page: action, method, named fields and submit buttons."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self.button: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "form":
            self.forms.append(
                {
                    "action": values.get("action", ""),
                    "method": values.get("method", "get").upper(),
                    "fields": {},
                    "buttons": [],
                }
            )
        elif not self.forms:
            return
        elif tag == "button" or tag == "input" and values.get("type") in {"submit", "button"}:
            # Scripts may carry the chosen answer: keep onclick/data-*/id as hints.
            hint = " ".join(
                value
                for key, value in values.items()
                if value and (key in {"onclick", "id", "formaction"} or key.startswith("data-"))
            )
            self.button = {
                "name": values.get("name", ""),
                "value": values.get("value", ""),
                "text": "",
                "hint": hint[:120],
                "id": values.get("id", ""),
            }
            self.forms[-1]["buttons"].append(self.button)
            if tag == "input":
                self.button = None
        elif tag == "input" and values.get("name"):
            self.forms[-1]["fields"][values["name"]] = values.get("value", "")

    def handle_data(self, data: str) -> None:
        if self.button is not None:
            self.button["text"] += data.strip()

    def handle_endtag(self, tag: str) -> None:
        if tag == "button":
            self.button = None

    def summary(self) -> str:
        # Paths, field names and button labels; only the "action" field value is shown,
        # other values may be CSRF tokens.
        return "; ".join(
            f"{form['method']} {urlsplit(form['action']).path or '.'} "
            f"fields={sorted(form['fields'])} action={form['fields'].get('action')!r} "
            "buttons="
            + str(
                [
                    f"{button['text'] or button['value']}"
                    f"[name={button['name']!r} value={button['value']!r} {button['hint']!r}]"
                    for button in form["buttons"]
                ]
            )
            for form in self.forms
        )


def trusting(button: dict[str, str]) -> bool:
    label = (button["text"] + " " + button["value"]).lower()
    return "довер" in label and "не довер" not in label


def trust_answers(button: dict[str, str]) -> list[str]:
    """Values a page script may put into the form's action field for this button."""
    found = [
        button["value"],
        button["id"],
        *re.findall(r"['\"]([A-Za-z][\w-]*)['\"]", button["hint"]),
    ]
    if trusting(button):
        found += ["trust", "true", "yes"]
    return list(dict.fromkeys(value for value in found if value))


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
        if state.sso_cookies and grant == "auto":
            return await self.web_renew(state)
        if not state.client_id or not state.client_secret:
            raise MeshAuthError("Нет данных устройства mos.ru. Выполните web-login.")
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
        client_id, client_secret = registration["client_id"], registration["client_secret"]
        state = AuthState(client_id=SecretStr(client_id), client_secret=SecretStr(client_secret))
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
                "client_id": client_id,
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
            auth=httpx.BasicAuth(client_id, client_secret),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        return await self.exchange(state, mos, via="authorization_code")

    async def web_login(
        self, username: str, password: str, ask_otp: Callable[[], str]
    ) -> AuthState:
        """Sign in as the school.mos.ru website; keep the mos.ru session, never the password."""
        cookies = self.transport.client.cookies
        cookies.clear()
        try:
            initial = await self.transport.json(
                "GET",
                AUTH_BASE + "/sps/oauth/ae",
                params={**WEB_AUTHORIZATION, "display": "script"},
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
            code = await self._authorization_code(response, ask_otp, WEB_REDIRECT_URI)
            token = await self._school_token(code)
            return await self._web_state(AuthState(), token, via="web_login")
        finally:
            cookies.clear()

    async def web_renew(self, state: AuthState) -> AuthState:
        """Get a fresh MESH token the way the website does, from the stored mos.ru session."""
        if not state.sso_cookies:
            raise MeshAuthError("Нет сессии mos.ru. Выполните web-login.")
        cookies = self.transport.client.cookies
        cookies.clear()
        try:
            for cookie in json.loads(state.sso_cookies.get_secret_value()):
                cookies.set(cookie["name"], cookie["value"], cookie["domain"], cookie["path"])
            response = await self.transport.request(
                "GET", AUTH_BASE + "/sps/oauth/ae", params=WEB_AUTHORIZATION
            )
            target = self._redirect(response)
            for _ in range(4):
                if target is None or target.path != "/sps/oauth/ae":
                    break
                response = await self.transport.request("GET", target.geturl())
                target = self._redirect(response)
            codes = (
                parse_qs(target.query).get("code")
                if target and target.netloc == "school.mos.ru"
                else None
            )
            if not codes or not codes[0]:
                # mos.ru asks for a password again: the session ended or was revoked.
                raise MeshAuthError("Сессия mos.ru истекла. Выполните web-login.")
            token = await self._school_token(codes[0])
            return await self._web_state(state, token, via="sso_session")
        finally:
            cookies.clear()

    def _redirect(self, response: httpx.Response) -> SplitResult | None:
        """Only the web callback and login.mos.ru OAuth steps are followed, never others."""
        location = response.headers.get("location")
        if not location:
            return None
        target = urlsplit(str(response.url.join(location)))
        if target.scheme != "https" or (
            target.netloc != "login.mos.ru"
            and (target.netloc, target.path) != ("school.mos.ru", "/v3/auth/sudir/callback")
        ):
            raise MeshAuthError("Неизвестный redirect авторизации; нужна проверка flow.")
        return target

    async def _school_token(self, code: str) -> str:
        """school.mos.ru redeems the code itself and answers with the aupd_token cookie."""
        response = await self.transport.request("GET", WEB_REDIRECT_URI, params={"code": code})
        for cookie in self.transport.client.cookies.jar:
            if cookie.name == "aupd_token" and cookie.value:
                return str(cookie.value)
        raise MeshAuthError(
            f"school.mos.ru не выдал токен после входа: HTTP {response.status_code}."
        )

    async def _web_state(self, state: AuthState, token: str, *, via: str) -> AuthState:
        session = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.transport.client.cookies.jar
            if c.domain.lstrip(".").endswith("mos.ru") and c.name != "aupd_token" and c.value
        ]
        # mos.ru may rotate its session cookies; persist them before activation can fail.
        state.sso_cookies = SecretStr(json.dumps(session, ensure_ascii=False))
        state.client_id = state.client_secret = state.refresh_token = None
        if state.mesh_access_token:
            self.store.save(state)
        await self.activate(token)
        state.mesh_access_token = SecretStr(token)
        state.mesh_expires_at = token_expiry(token)
        state.mos_expires_in = None
        state.refreshed_at = datetime.now(UTC).isoformat()
        state.refresh_via = via
        state.activated = True
        self.store.save(state)
        return state

    async def _answer_trust(self, response: httpx.Response) -> httpx.Response:
        """Trust this device (a longer mos.ru session); decline only if trust is refused."""
        page = PageForms()
        page.feed(response.text)
        if not page.forms:
            logger.warning("mos.ru: вопрос о доверии без формы (HTML %s байт)", len(response.text))
            return await self.transport.request(
                "GET", AUTH_BASE + "/sps/oauth/ae", params=WEB_AUTHORIZATION
            )
        tried = []
        for form in page.forms:
            target = urlsplit(str(response.url.join(form["action"] or response.url.path)))
            if (target.scheme, target.netloc) != ("https", "login.mos.ru"):
                continue
            # Trusting buttons first; a decline still completes the login.
            for button in sorted(form["buttons"], key=lambda item: not trusting(item)):
                if button["name"]:
                    attempts = [{button["name"]: button["value"]}]
                elif "action" in form["fields"]:
                    attempts = [{"action": answer} for answer in trust_answers(button)]
                else:
                    continue
                for answer in attempts[:6]:
                    data = {**form["fields"], **answer}
                    if form["method"] == "POST":
                        reply = await self.transport.request("POST", target.geturl(), data=data)
                    else:
                        reply = await self.transport.request("GET", target.geturl(), params=data)
                    tried.append(f"{answer}:{reply.status_code}")
                    if reply.is_redirect:
                        logger.warning(
                            "mos.ru: %s (%s)",
                            "устройство отмечено доверенным"
                            if trusting(button)
                            else "доверие отклонено, сессия будет короче",
                            ", ".join(tried),
                        )
                        return reply
        # Without an answer mos.ru asks for the password again, so stop with details.
        raise MeshAuthError(
            f"mos.ru не принял ответ о доверии устройству: {page.summary()}; "
            f"попытки {', '.join(tried) or 'нет'}"
        )

    async def _authorization_code(
        self,
        response: httpx.Response,
        ask_otp: Callable[[], str],
        redirect_uri: str = REDIRECT_URI,
    ) -> str:
        expected = urlsplit(redirect_uri)
        sms_requested = False
        otp_submitted = False
        trust_requested = False
        for _ in range(12):
            try:
                data = response.json()
            except ValueError:
                data = {}
            location = response.headers.get("location", "")
            if (
                not location
                and isinstance(data, dict)
                and isinstance(data.get("redirect_url"), str)
            ):
                location = data["redirect_url"]
            parsed = urlsplit(str(response.url.join(location))) if location else urlsplit("")
            is_callback = (parsed.scheme, parsed.netloc, parsed.path) == (
                expected.scheme,
                expected.netloc,
                expected.path,
            )
            # Never follow arbitrary redirects with credentials or cookies. The web flow
            # continues on login.mos.ru itself (OAuth resume, trust prompt) before the callback.
            if (
                location
                and not is_callback
                and parsed.scheme == "https"
                and parsed.netloc == "login.mos.ru"
                and expected.scheme == "https"
            ):
                response = await self.transport.request("GET", parsed.geturl())
                continue
            if location and not is_callback:
                raise MeshAuthError(
                    "Неизвестный redirect авторизации; нужна проверка flow: "
                    f"{parsed.netloc}{parsed.path}"
                )
            codes = parse_qs(parsed.query).get("code")
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
            if (
                expected.scheme == "https"
                and response.url.path == "/sps/login/ur/askToTrust"
                and not trust_requested
            ):
                trust_requested = True
                response = await self._answer_trust(response)
                continue
            if otp_submitted and "Доверять" in response.text and not trust_requested:
                trust_requested = True
                response = await self.transport.request(
                    "GET",
                    AUTH_BASE + "/sps/login/ur/askToTrust",
                )
                continue
            # Field names and inquire values only: enough to adapt the flow, no secrets.
            raise MeshAuthError(
                "Неподдерживаемый шаг входа (например, CAPTCHA/согласие): "
                f"HTTP {response.status_code} {response.url.path} "
                f"inquire={data.get('inquire')} fields={sorted(data)[:10]}"
            )
        raise MeshAuthError("Превышено ограничение числа шагов авторизации.")
