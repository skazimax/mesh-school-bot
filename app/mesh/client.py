"""Low-level MESH endpoints used by the normalized client and diagnostic CLI."""

from datetime import date
from typing import Any

from app.mesh.auth import SCHOOL_BASE, ResearchAuth
from app.mesh.exceptions import MeshAPIError, MeshConfigError
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport


def object_payload(data: Any, *, key: str = "payload") -> list[dict[str, Any]]:
    values = data.get(key) if isinstance(data, dict) else data
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise MeshAPIError(f"API изменился: ожидался список объектов в {key}.")
    return values


def profile_role(value: Any) -> str:
    roles = {
        "parent": "parent",
        "ParentProfile": "parent",
        "student": "student",
        "StudentProfile": "student",
    }
    if not isinstance(value, str) or value not in roles:
        raise MeshConfigError("Неподдерживаемый тип профиля МЭШ.")
    return roles[value]


class ResearchClient:
    def __init__(
        self,
        transport: Transport,
        store: AuthStore,
        *,
        profile_id: str | None = None,
        student_id: str | None = None,
        api: str = "mobile",
        date_params: str = "from-to",
    ) -> None:
        state = store.load()
        if not state.mesh_access_token or not state.activated:
            raise MeshConfigError("Нет активированного токена МЭШ. Выполните login или refresh.")
        self.token = state.mesh_access_token.get_secret_value()
        self.transport = transport
        self.auth = ResearchAuth(transport, store)
        self.profile_id = profile_id
        self.student_id = student_id
        self.api = api
        self.date_params = date_params
        self.profile_type = "parent"

    def headers(self) -> dict[str, str]:
        headers = {
            "Auth-Token": self.token,
            "Authorization": "Bearer " + self.token,
            "Cookie": f"aupdtoken={self.token}; aupd_token={self.token}",
            "X-Mes-Subsystem": "familymp" if self.api == "mobile" else "familyweb",
        }
        if self.api == "mobile":
            headers["client-type"] = "diary-mobile"
        else:
            headers["Profile-Type"] = self.profile_type
        if self.profile_id:
            headers["Profile-Id"] = self.profile_id
        return headers

    async def get_profile(self) -> dict[str, Any]:
        profiles = await self.auth.activate(self.token)
        parents = [item for item in profiles if item.get("type") in {"parent", "ParentProfile"}]
        candidates = parents or profiles
        if self.profile_id:
            selected = next((p for p in profiles if str(p["id"]) == self.profile_id), None)
            if not selected:
                raise MeshConfigError("MESH_PROFILE_ID отсутствует среди доступных профилей.")
        elif len(candidates) == 1:
            selected = candidates[0]
            self.profile_id = str(selected["id"])
        else:
            raise MeshConfigError("Доступны несколько профилей. Укажите MESH_PROFILE_ID.")
        self.profile_type = profile_role(selected.get("type"))
        family = await self.transport.json(
            "GET",
            f"{SCHOOL_BASE}/api/family/{self.api}/v1/profile",
            headers=self.headers(),
        )
        if not isinstance(family, dict) or not isinstance(family.get("profile"), dict):
            raise MeshAPIError("API изменился: нет объекта profile в семейном профиле.")
        children = family.get("children")
        if not isinstance(children, list) or not all(
            isinstance(child, dict) and child.get("id") for child in children
        ):
            raise MeshAPIError("API изменился: нет списка children в семейном профиле.")
        return {"profiles": profiles, "family": family}

    def select_student(self, profile: dict[str, Any]) -> dict[str, Any]:
        children: list[dict[str, Any]] = profile["family"]["children"]
        if self.student_id:
            child = next((item for item in children if str(item["id"]) == self.student_id), None)
            if not child:
                raise MeshConfigError("MESH_STUDENT_ID отсутствует среди детей этого профиля.")
        elif len(children) == 1:
            child = children[0]
            self.student_id = str(child["id"])
        else:
            raise MeshConfigError(
                "Выберите ребёнка через MESH_STUDENT_ID; сначала выполните profile."
            )
        return child

    async def fetch(
        self,
        resource: str,
        start: date,
        end: date,
        *,
        short_averages: bool = False,
        person_id: str | None = None,
    ) -> Any:
        if start > end:
            raise MeshConfigError("Начальная дата позже конечной.")
        if not self.profile_id or not self.student_id:
            raise MeshConfigError("Сначала получите профиль и выберите ребёнка.")
        params = {"student_id": self.student_id}
        if resource == "schedule":
            if not person_id:
                raise MeshConfigError("В профиле ребёнка нет person_id/contingent_guid.")
            return await self.transport.json(
                "GET",
                SCHOOL_BASE + "/api/eventcalendar/v1/api/events",
                params={
                    "person_ids": person_id,
                    "begin_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "expand": "marks,homework",
                },
                headers={**self.headers(), "x-mes-role": self.profile_type},
            )
        if resource == "averages":
            endpoint = "subject_marks/short" if short_averages else "subject_marks"
        elif resource in {"marks", "homework", "periods"}:
            endpoint = {
                "marks": "marks",
                "homework": "homeworks/short" if self.api == "mobile" else "homeworks",
                "periods": "periods_schedules",
            }[resource]
            keys = ("from", "to") if self.date_params == "from-to" else ("from_date", "to_date")
            params.update({keys[0]: start.isoformat(), keys[1]: end.isoformat()})
        else:
            raise MeshConfigError("Неизвестный ресурс API.")
        # mesh-diary uses query profile_id, unlike OctoDiary's header-only variant.
        if self.date_params == "from_date-to_date":
            params["profile_id"] = self.profile_id
        return await self.transport.json(
            "GET",
            f"{SCHOOL_BASE}/api/family/{self.api}/v1/{endpoint}",
            params=params,
            headers=self.headers(),
        )
