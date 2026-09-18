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
