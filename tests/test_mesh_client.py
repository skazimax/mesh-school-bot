import asyncio
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from app.mesh.exceptions import MeshAPIError
from app.mesh.models import AuthState, Student
from app.mesh.normalized import MobileMeshClient
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport


async def test_concurrent_401_refreshes_once(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.save(
        AuthState(
            client_id=SecretStr("id"),
            client_secret=SecretStr("secret"),
            mesh_access_token=SecretStr("old"),
            activated=True,
        )
    )
    refresh_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sps/oauth/te":
            refresh_calls.append(parse_qs(request.content.decode()))
            return httpx.Response(200, json={"access_token": "mos"})
        if request.url.path == "/v3/auth/sudir/auth":
            return httpx.Response(
                200,
                json={"user_authentication_for_mobile_response": {"mesh_access_token": "fresh"}},
            )
        if request.url.path.endswith("profile_info"):
            return httpx.Response(200, json=[{"id": 1, "type": "ParentProfile"}])
        if request.headers.get("Auth-Token") == "old":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"payload": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mesh = MobileMeshClient(Transport(http), store)
        student = Student(id="2", profile_id="1", name="Test")
        today = date(2026, 9, 18)
        await asyncio.gather(
            mesh.get_marks(student, today, today), mesh.get_marks(student, today, today)
        )
    assert len(refresh_calls) == 1


async def test_normalized_client_rejects_ignored_date_filter(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.save(
        AuthState(
            client_id=SecretStr("id"),
            client_secret=SecretStr("secret"),
            mesh_access_token=SecretStr("old"),
            activated=True,
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"payload": [{"id": 1, "date": "2026-09-18", "value": "5"}]}
            )
        )
    ) as http:
        mesh = MobileMeshClient(Transport(http), store)
        with pytest.raises(MeshAPIError, match="фильтр дат"):
            await mesh.get_marks(
                Student(id="2", profile_id="1", name="Test"), date(2026, 9, 15), date(2026, 9, 15)
            )


async def test_unknown_current_period_never_uses_all_year_average(tmp_path: Path) -> None:
    store = AuthStore(tmp_path / "auth.json")
    store.save(
        AuthState(
            client_id=SecretStr("id"),
            client_secret=SecretStr("secret"),
            mesh_access_token=SecretStr("token"),
            activated=True,
        )
    )
    payload = {
        "payload": [
            {"subject_id": "3", "subject_name": "Математика", "average": "4.99", "periods": []}
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload),
        )
    ) as http:
        averages = await MobileMeshClient(Transport(http), store).get_subject_averages(
            Student(id="1", profile_id="p", name="Test"),
            date(2026, 9, 18),
        )
    assert averages[0].period_id == "unknown"
    assert averages[0].average is None
