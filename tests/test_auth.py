"""Mock-only regression tests. These are NOT proof of a working MESH integration."""

import hashlib
import json
import stat
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.mesh.auth import ResearchAuth, solve_hashcash, token_expiry
from app.mesh.client import ResearchClient, profile_role
from app.mesh.exceptions import MeshAPIError, MeshAuthError, MeshConfigError
from app.mesh.models import AuthState, RegistrationMetadata
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport
from tools.mesh import validate_rows
from tools.mesh_verify import summarize


@pytest.fixture
def store(tmp_path: Path) -> AuthStore:
    result = AuthStore(tmp_path / "secrets" / "auth.json")
    result.save(
        AuthState(
            client_id=SecretStr("device-id"),
            client_secret=SecretStr("device-secret"),
            refresh_token=SecretStr("old-refresh"),
            mesh_access_token=SecretStr("old-mesh"),
            activated=True,
        )
    )
    return result


def test_auth_storage_private_and_repr_redacted(store: AuthStore) -> None:
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    state = store.load()
    assert state.client_secret.get_secret_value() == "device-secret"
    assert "device-secret" not in repr(state)
    assert "old-refresh" not in repr(state)


def test_hashcash_checks_leading_bits() -> None:
    header = "1:15:260918:example::salt:"
    stamp = solve_hashcash(header)
    assert stamp.startswith(header)
    assert hashlib.sha1(stamp.encode(), usedforsecurity=False).digest()[:2] == b"\0\0"
    with pytest.raises(MeshAuthError):
        solve_hashcash("1:40:unsupported:")
    assert token_expiry("not-a-jwt") is None


async def test_client_credentials_exchange_activation(store: AuthStore) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/sps/oauth/te":
            assert parse_qs(request.content.decode())["grant_type"] == ["client_credentials"]
            return httpx.Response(200, json={"access_token": "mos-token", "expires_in": 3600})
        if request.url.path == "/v3/auth/sudir/auth":
            assert json.loads(request.content)["user_authentication_for_mobile_request"] == {
                "mos_access_token": "mos-token"
            }
            return httpx.Response(
                200,
                json={"user_authentication_for_mobile_response": {"mesh_access_token": "new-mesh"}},
            )
        assert request.headers["partner-source-id"] == "MOBILE"
        assert request.headers["auth-token"] == "new-mesh"
        return httpx.Response(200, json=[{"id": 1, "type": "parent"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        state = await ResearchAuth(Transport(http), store).refresh()
    assert state.refresh_via == "client_credentials"
    assert store.load().mesh_access_token.get_secret_value() == "new-mesh"
    assert calls == ["/sps/oauth/te", "/v3/auth/sudir/auth", "/acl/api/users/profile_info"]


@pytest.mark.parametrize("failure", ["exchange", "activation"])
async def test_rotation_survives_later_failure(store: AuthStore, failure: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sps/oauth/te":
            return httpx.Response(200, json={"access_token": "mos", "refresh_token": "rotated"})
        if request.url.path == "/v3/auth/sudir/auth":
            if failure == "exchange":
                return httpx.Response(503, text="server error; private-secret")
            return httpx.Response(
                200,
                json={
                    "user_authentication_for_mobile_response": {"mesh_access_token": "unactivated"}
                },
            )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises((MeshAPIError, MeshAuthError)):
            await ResearchAuth(Transport(http), store).refresh(grant="refresh_token")
    saved = store.load()
    assert saved.refresh_token.get_secret_value() == "rotated"
    assert saved.mesh_access_token.get_secret_value() == "old-mesh"


async def test_invalid_client_does_not_attempt_fallback(store: AuthStore) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json={"error": "invalid_client", "private": "secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MeshAuthError, match="invalid_client") as error:
            await ResearchAuth(Transport(http), store).refresh()
    assert len(calls) == 1
    assert "secret" not in str(error.value)


async def test_auto_fallback_and_refresh_rotation(store: AuthStore) -> None:
    grants = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sps/oauth/te":
            data = parse_qs(request.content.decode())
            grants.append(data["grant_type"][0])
            if grants[-1] == "client_credentials":
                return httpx.Response(400, json={"error": "unsupported_grant_type"})
            assert data["refresh_token"] == ["old-refresh"]
            return httpx.Response(200, json={"access_token": "mos", "refresh_token": "new-refresh"})
        if request.url.path == "/v3/auth/sudir/auth":
            return httpx.Response(
                200,
                json={"user_authentication_for_mobile_response": {"mesh_access_token": "new-mesh"}},
            )
        return httpx.Response(200, json=[{"id": 1}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        state = await ResearchAuth(Transport(http), store).refresh()
    assert grants == ["client_credentials", "refresh_token"]
    assert state.refresh_via == "refresh_token"
    assert store.load().refresh_token.get_secret_value() == "new-refresh"


async def test_parent_profile_and_child_ids_remain_distinct(store: AuthStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("profile_info"):
            return httpx.Response(
                200,
                json=[{"id": 111, "type": "ParentProfile"}, {"id": 222, "type": "StudentProfile"}],
            )
        assert request.headers["profile-id"] == "111"
        if request.url.path.endswith("/profile"):
            return httpx.Response(
                200,
                json={
                    "profile": {"id": 111},
                    "children": [{"id": 222, "contingent_guid": "child-guid"}, {"id": 333}],
                },
            )
        assert request.url.params["student_id"] == "222"
        assert request.url.params["from"] == "2026-09-01"
        assert "from_date" not in request.url.params
        return httpx.Response(200, json={"payload": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = ResearchClient(Transport(http), store)
        profile = await client.get_profile()
        assert client.profile_type == "parent"
        with pytest.raises(MeshConfigError, match="Выберите ребёнка"):
            client.select_student(profile)
        client.student_id = "222"
        child = client.select_student(profile)
        assert child["contingent_guid"] == "child-guid"
        await client.fetch("marks", date(2026, 9, 1), date(2026, 9, 18))


def test_profile_roles_handle_current_api_types() -> None:
    assert profile_role("ParentProfile") == "parent"
    assert profile_role("StudentProfile") == "student"
    with pytest.raises(MeshConfigError):
        profile_role("UnknownProfile")


def test_ignored_date_filter_is_detected() -> None:
    stats = summarize(
        "marks", [{"id": 1, "date": "2026-09-18"}], date(2026, 9, 15), date(2026, 9, 15)
    )
    assert stats["outside_requested_range"] == 1
    assert stats["unparseable_dates"] == 0


async def test_login_pkce_sms_and_activation(store: AuthStore) -> None:
    code_challenge = ""
    otp_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal code_challenge
        path = request.url.path
        if path == "/sps/oauth/register":
            return httpx.Response(
                200, json={"client_id": "new-device", "client_secret": "new-secret"}
            )
        if path == "/sps/oauth/ae":
            code_challenge = request.url.params["code_challenge"]
            return httpx.Response(200, json={"items": [{"inquire": "login_with_password"}]})
        if path.endswith("/password"):
            assert json.loads(request.content)["password"] == "test-password"
            return httpx.Response(200, json={"items": [{"inquire": "ask_to_send_sms"}]})
        if path.endswith("/sms/bind"):
            if request.content:
                assert parse_qs(request.content.decode())["sms-code"] == ["123456"]
                return httpx.Response(
                    302,
                    headers={"location": "dnevnik-mes://oauth2redirect?code=code123&state=unused"},
                )
            return httpx.Response(200, json={"inquire": "enter_sms_code"})
        if path == "/sps/oauth/te":
            import base64

            data = parse_qs(request.content.decode())
            verifier = data["code_verifier"][0]
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            assert expected.decode().rstrip("=") == code_challenge
            assert data["code"] == ["code123"]
            return httpx.Response(200, json={"access_token": "mos", "refresh_token": "refresh"})
        if path == "/v3/auth/sudir/auth":
            return httpx.Response(
                200, json={"user_authentication_for_mobile_response": {"mesh_access_token": "mesh"}}
            )
        return httpx.Response(200, json=[{"id": 111, "type": "parent"}])

    def ask_otp() -> str:
        otp_calls.append(True)
        return "123456"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as http:
        state = await ResearchAuth(Transport(http), store).login(
            RegistrationMetadata(
                bearer=SecretStr("public-app-bearer"),
                software_statement=SecretStr("public-statement"),
                source="test",
            ),
            "test-login",
            "test-password",
            ask_otp,
        )
    assert state.activated
    assert otp_calls == [True]
    assert "test-password" not in store.path.read_text()
    assert "123456" not in store.path.read_text()


async def test_post_not_retried_and_errors_redacted() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="PRIVATE_TOKEN_VALUE")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MeshAPIError) as error:
            await Transport(http).json("POST", "https://login.mos.ru/sps/oauth/te?secret=SECRET")
    assert len(requests) == 1
    assert "PRIVATE_TOKEN_VALUE" not in str(error.value)
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("data", [None, {}, {"payload": {}}, {"payload": ["unexpected"]}])
def test_response_shape_changes_fail_loudly(data: Any) -> None:
    with pytest.raises(MeshAPIError):
        validate_rows("marks", data)


def test_missing_averages_not_successful_smoke_test() -> None:
    with pytest.raises(MeshConfigError, match="нет средних"):
        validate_rows("averages", {"payload": [{"subject_id": 1, "average": None}]})


async def test_untrusted_redirect_is_not_followed(store: AuthStore) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as http:
        response = httpx.Response(
            302,
            headers={"location": "https://evil.example/?code=secret"},
            request=httpx.Request("POST", "https://login.mos.ru/password"),
        )
        with pytest.raises(MeshAuthError, match="Неизвестный redirect"):
            await ResearchAuth(Transport(http), store)._authorization_code(response, lambda: "123")


TRUST_FORM = """<form method="post" action="/sps/login/ur/trust">
<input type="hidden" name="csrf" value="c-1"><button name="answer" value="yes">Доверять</button>
<button name="answer" value="no">Не доверять</button></form>"""
# As on login.mos.ru: a script fills the hidden action field from the pressed button.
TRUST_SCRIPT_FORM = """<form method="post" action="/sps/login/ur/askToTrust">
<input type="hidden" name="action" value="">
<button id="agree" data-loading-text="Выполнение операции ...">Доверять</button>
<button id="disagree" data-loading-text="Выполнение операции ...">Не доверять</button></form>"""


def web_handler(  # type: ignore[no-untyped-def]
    calls: list[str],
    *,
    session: str = "s2",
    token: str = "web-mesh",
    trust_page: str = "",
    accept: frozenset[str] = frozenset({"agree"}),
):
    """login.mos.ru issues the session; school.mos.ru redeems the code into aupd_token."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host + request.url.path)
        if request.url.path == "/sps/oauth/ae":
            assert request.url.params["client_id"] == "dnevnik.mos.ru"
            cookie = {"set-cookie": f"session-cookie={session}; Domain=login.mos.ru; Path=/"}
            if request.url.params.get("display") == "script":
                return httpx.Response(
                    200, headers=cookie, json={"items": [{"inquire": "login_with_password"}]}
                )
            if "session-cookie=s1" not in request.headers.get("cookie", ""):
                return httpx.Response(303, headers={"location": "/sps/login/methods/password"})
            return httpx.Response(
                303,
                headers={
                    **cookie,
                    "location": "https://school.mos.ru/v3/auth/sudir/callback?code=renewed",
                },
            )
        if request.url.path.endswith("/password"):
            assert json.loads(request.content)["password"] == "test-password"
            return httpx.Response(200, json={"items": [{"inquire": "ask_to_send_sms"}]})
        if request.url.path.endswith("/sms/bind"):
            if not request.content:
                return httpx.Response(200, json={"inquire": "enter_sms_code"})
            return httpx.Response(302, headers={"location": "/sps/login/ur/askToTrust"})
        if request.url.path == "/sps/login/ur/askToTrust" and request.method == "POST":
            answer = parse_qs(request.content.decode()).get("action", [""])[0]
            if answer not in accept:
                return httpx.Response(404)
            calls.append("trusted" if answer == "agree" else "declined")
            return httpx.Response(
                302, headers={"location": "/sps/oauth/ae?client_id=dnevnik.mos.ru"}
            )
        if request.url.path == "/sps/login/ur/askToTrust":
            return httpx.Response(200, html=trust_page)
        if request.url.path == "/sps/login/ur/trust":
            calls.append("trusted")
            assert parse_qs(request.content.decode()) == {"csrf": ["c-1"], "answer": ["yes"]}
            return httpx.Response(
                302, headers={"location": "/sps/oauth/ae?client_id=dnevnik.mos.ru"}
            )
        if request.url.path == "/v3/auth/sudir/callback":
            return httpx.Response(
                302,
                headers={
                    "set-cookie": f"aupd_token={token}; Domain=school.mos.ru; Path=/",
                    "location": "/",
                },
            )
        assert request.headers["auth-token"] == token
        return httpx.Response(200, json=[{"id": 111, "type": "parent"}])

    return handler


@pytest.mark.parametrize(
    "trust_page,accept,outcome",
    [
        (TRUST_FORM, frozenset(), "trusted"),
        (TRUST_SCRIPT_FORM, frozenset({"agree"}), "trusted"),
        (TRUST_SCRIPT_FORM, frozenset({"disagree"}), "declined"),
        ("<div id=root></div>", frozenset(), None),
    ],
)
async def test_web_login_keeps_session_not_password(
    store: AuthStore, trust_page: str, accept: frozenset[str], outcome: str | None
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sps/oauth/ae" and "display" not in request.url.params:
            # After the SMS step mos.ru resumes OAuth with the session it just set.
            assert "session-cookie=s2" in request.headers.get("cookie", "")
            return httpx.Response(
                302, headers={"location": "https://school.mos.ru/v3/auth/sudir/callback?code=c1"}
            )
        return web_handler(calls, trust_page=trust_page, accept=accept)(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as http:
        state = await ResearchAuth(Transport(http), store).web_login(
            "test-login", "test-password", lambda: "123456"
        )
        assert not http.cookies.jar, "a shared HTTP client must not keep the session"
    saved = store.load()
    assert saved.mesh_access_token.get_secret_value() == "web-mesh"  # type: ignore[union-attr]
    assert state.refresh_via == "web_login" and saved.activated
    assert saved.client_id is None and saved.refresh_token is None
    session = json.loads(saved.sso_cookies.get_secret_value())  # type: ignore[union-attr]
    assert [(c["name"], c["value"]) for c in session] == [("session-cookie", "s2")]
    text = store.path.read_text()
    assert "test-password" not in text and "123456" not in text
    assert "school.mos.ru/v3/auth/sudir/callback" in calls
    # Trust is accepted when offered; without a form login still completes.
    assert [call for call in calls if call in {"trusted", "declined"}] == (
        [outcome] if outcome else []
    )


async def test_rejected_trust_answers_stop_with_details(store: AuthStore) -> None:
    handler = web_handler([], trust_page=TRUST_SCRIPT_FORM, accept=frozenset())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as http:
        with pytest.raises(MeshAuthError, match="agree.*404.*disagree.*404"):
            await ResearchAuth(Transport(http), store).web_login("u", "test-password", lambda: "1")
    assert store.load().mesh_access_token.get_secret_value() == "old-mesh"  # type: ignore[union-attr]


def web_store(store: AuthStore) -> AuthStore:
    store.save(
        AuthState(
            sso_cookies=SecretStr(
                json.dumps(
                    [
                        {
                            "name": "session-cookie",
                            "value": "s1",
                            "domain": "login.mos.ru",
                            "path": "/",
                        }
                    ]
                )
            ),
            mesh_access_token=SecretStr("old-web-mesh"),
            activated=True,
        )
    )
    return store


async def test_web_session_renews_token_and_rotates_cookies(store: AuthStore) -> None:
    calls: list[str] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(web_handler(calls, token="fresh")), follow_redirects=False
    ) as http:
        state = await ResearchAuth(Transport(http), web_store(store)).refresh()
    assert state.refresh_via == "sso_session"
    saved = store.load()
    assert saved.mesh_access_token.get_secret_value() == "fresh"  # type: ignore[union-attr]
    assert '"s2"' in saved.sso_cookies.get_secret_value()  # type: ignore[union-attr]
    assert calls == [
        "login.mos.ru/sps/oauth/ae",
        "school.mos.ru/v3/auth/sudir/callback",
        "dnevnik.mos.ru/acl/api/users/profile_info",
    ]


async def test_expired_web_session_asks_for_login_and_keeps_token(store: AuthStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(303, headers={"location": "/sps/login/methods/password?bo=x"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as http:
        with pytest.raises(MeshAuthError, match="web-login"):
            await ResearchAuth(Transport(http), web_store(store)).refresh()
    assert store.load().mesh_access_token.get_secret_value() == "old-web-mesh"  # type: ignore[union-attr]


async def test_web_session_never_follows_foreign_redirect(store: AuthStore) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/cb?code=stolen"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as http:
        with pytest.raises(MeshAuthError, match="Неизвестный redirect"):
            await ResearchAuth(Transport(http), web_store(store)).refresh()
