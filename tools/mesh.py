"""Interactive MESH authorization and safe API diagnostics."""

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import re
import ssl
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv

from app.mesh.auth import USER_AGENT, ResearchAuth
from app.mesh.client import ResearchClient, object_payload
from app.mesh.exceptions import MeshConfigError, MeshError
from app.mesh.storage import AuthStore, load_registration, save_private_json
from app.mesh.transport import Transport

REFERENCE_COMMIT = "319c44ff25a156dacd3afdf2773959db31ca29dc"
REGISTRATION_SOURCE = (
    f"https://raw.githubusercontent.com/voenniy/mesh-diary/{REFERENCE_COMMIT}/lib/auth.js"
)
REGISTRATION_SHA256 = "bdfab7391eabe7ad56968d2d34bdc6dca50b098b8b8f55ebbd22ce6518ed68cf"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "command",
        choices=[
            "prepare-login",
            "login",
            "refresh",
            "profile",
            "marks",
            "homework",
            "averages",
            "schedule",
            "periods",
            "check",
        ],
    )
    result.add_argument(
        "--auth-file", type=Path, default=Path(os.getenv("MESH_AUTH_FILE") or ".secrets/auth.json")
    )
    result.add_argument(
        "--registration-file",
        type=Path,
        default=Path(os.getenv("MESH_REGISTRATION_FILE") or ".secrets/registration.json"),
    )
    result.add_argument("--profile-id", default=os.getenv("MESH_PROFILE_ID") or None)
    result.add_argument("--student-id", default=os.getenv("MESH_STUDENT_ID") or None)
    result.add_argument("--api", choices=["mobile", "web"], default="mobile")
    result.add_argument(
        "--date-params", choices=["from-to", "from_date-to_date"], default="from-to"
    )
    result.add_argument("--from", dest="from_date", help="YYYY-MM-DD; homework: today; else: -14d")
    result.add_argument("--to", dest="to_date", help="YYYY-MM-DD; homework: +7d; else: today")
    result.add_argument("--short-averages", action="store_true")
    result.add_argument(
        "--grant", choices=["auto", "client_credentials", "refresh_token"], default="auto"
    )
    result.add_argument(
        "--show-data",
        action="store_true",
        help="Print personal API data locally; never use for auth responses",
    )
    result.add_argument("--verbose", action="store_true", help="Safe adapter logs only")
    return result


async def prepare_login(transport: Transport, path: Path) -> None:
    response = await transport.request("GET", REGISTRATION_SOURCE)
    if not response.is_success:
        raise MeshConfigError(f"Не удалось получить metadata: HTTP {response.status_code}.")
    if hashlib.sha256(response.content).hexdigest() != REGISTRATION_SHA256:
        raise MeshConfigError("SHA256 reference-файла не совпал. Metadata не сохранена.")
    values = {}
    for source_key, target_key in (
        ("BEARER_TOKEN", "bearer"),
        ("SOFTWARE_STATEMENT", "software_statement"),
    ):
        match = re.search(rf'const {source_key}\s*=\s*"([^"\n]+)";', response.text)
        if not match:
            raise MeshConfigError("Формат reference metadata изменился.")
        values[target_key] = match.group(1)
    save_private_json(path, {**values, "source": REGISTRATION_SOURCE})
    print("Metadata регистрации сохранена приватно; исходный JS не выполнялся.")


def validate_rows(resource: str, data: Any) -> list[dict[str, Any]]:
    rows = object_payload(data, key="response" if resource == "schedule" else "payload")
    required = {
        "marks": {"id", "value", "subject_id", "date"},
        "homework": {"subject_id", "description", "date", "homework_entry_student_id"},
        "averages": {"subject_id"},
    }.get(resource, set())
    if any(not required <= row.keys() for row in rows):
        raise MeshConfigError(f"Ответ {resource} не содержит ожидаемые поля; нужна проверка API.")
    if resource == "averages" and rows and not any(row.get("average") is not None for row in rows):
        raise MeshConfigError("В subject_marks нет средних average; исследование не завершено.")
    return rows


async def run(args: argparse.Namespace) -> int:
    ca_bundle = os.getenv("MESH_CA_BUNDLE") or None
    tls: ssl.SSLContext | bool = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
    timeout = float(os.getenv("MESH_HTTP_TIMEOUT") or "20")
    if not 0 < timeout <= 120:
        raise MeshConfigError("MESH_HTTP_TIMEOUT должен быть в диапазоне (0, 120].")
    async with httpx.AsyncClient(
        timeout=timeout,
        verify=tls,
        proxy=os.getenv("MESH_PROXY") or None,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=False,
        trust_env=False,
    ) as http:
        transport = Transport(http)
        store = AuthStore(args.auth_file)
        auth = ResearchAuth(transport, store)
        if args.command == "prepare-login":
            await prepare_login(transport, args.registration_file)
            return 0
        if args.command in {"login", "refresh"}:
            if args.command == "login":
                metadata = load_registration(args.registration_file)
                if not sys.stdin.isatty():
                    raise MeshConfigError("login требует интерактивный терминал для пароля и SMS.")
                username = (
                    os.getenv("MESH_LOGIN")
                    or (await asyncio.to_thread(input, "Логин mos.ru: ")).strip()
                )
                if not username:
                    raise MeshConfigError("Логин пуст.")
                password = await asyncio.to_thread(
                    getpass.getpass, "Пароль mos.ru (не сохраняется): "
                )
                state = await auth.login(
                    metadata, username, password, lambda: getpass.getpass("SMS-код: ")
                )
            else:
                state = await auth.refresh(grant=args.grant)
            print(
                json.dumps(
                    {
                        "activated": state.activated,
                        "via": state.refresh_via,
                        "mos_expires_in": state.mos_expires_in,
                        "mesh_expires_at": state.mesh_expires_at,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        today = datetime.now(ZoneInfo(os.getenv("TZ") or "Europe/Moscow")).date()
        start = (
            datetime.strptime(args.from_date, "%Y-%m-%d").date()
            if args.from_date
            else (today if args.command == "homework" else today - timedelta(days=14))
        )
        end = (
            datetime.strptime(args.to_date, "%Y-%m-%d").date()
            if args.to_date
            else (today + timedelta(days=7) if args.command == "homework" else today)
        )
        if start > end:
            raise MeshConfigError("Начальная дата позже конечной.")
        client = ResearchClient(
            transport,
            store,
            profile_id=args.profile_id,
            student_id=args.student_id,
            api=args.api,
            date_params=args.date_params,
        )
        profile = await client.get_profile()
        children = profile["family"]["children"]
        if args.command == "profile":
            profile_summary = {
                "profile_id": client.profile_id,
                "profile_type": client.profile_type,
                "children": [
                    {
                        "student_id": child["id"],
                        "person_id": child.get("contingent_guid") or child.get("person_id"),
                    }
                    for child in children
                ],
            }
            print(
                json.dumps(
                    profile if args.show_data else profile_summary, ensure_ascii=False, indent=2
                )
            )
            return 0
        child = client.select_student(profile)
        person_id = child.get("contingent_guid") or child.get("person_id")
        resources = ["marks", "averages", "homework"] if args.command == "check" else [args.command]
        counts = {}
        for resource in resources:
            data = await client.fetch(
                resource, start, end, short_averages=args.short_averages, person_id=person_id
            )
            rows = validate_rows(resource, data)
            counts[resource] = len(rows)
            summary: dict[str, Any] = {
                "resource": resource,
                "api": args.api,
                "date_params": args.date_params,
                "count": len(rows),
                "sample_fields": sorted(rows[0]) if rows else [],
                "empty_response": not rows,
            }
            if resource == "averages":
                summary["date_scope"] = "periods_from_api; no date filter"
            else:
                summary.update({"from": start.isoformat(), "to": end.isoformat()})
            print(json.dumps(data if args.show_data else summary, ensure_ascii=False, indent=2))
        if args.command == "check":
            if any(count == 0 for count in counts.values()):
                print(
                    "Есть пустые ответы. Выберите даты с известными оценками и ДЗ; "
                    "Получение данных ещё не подтверждено.",
                    file=sys.stderr,
                )
                return 3
            print(
                "Реальные непустые ответы profile/marks/averages/homework получены. "
                "Проверьте содержимое и границы дат; refresh проверяется отдельно."
            )
        return 0


def main() -> int:
    load_dotenv(".env", override=False)
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    # httpx INFO logs include query strings; never enable them, even with --verbose.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        return asyncio.run(run(args))
    except (MeshError, OSError, ValueError, ZoneInfoNotFoundError, EOFError) as exc:
        # Config/SSL exceptions can contain file names but no auth response bodies.
        if isinstance(exc, MeshError):
            print(str(exc), file=sys.stderr)
        else:
            print(
                f"Ошибка конфигурации ({type(exc).__name__}); проверьте даты, TZ, файлы и TLS.",
                file=sys.stderr,
            )
        return 2
    except KeyboardInterrupt:
        print("Отменено.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
