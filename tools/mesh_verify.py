"""Read-only, anonymized real-account verification for every child in a family."""

import argparse
import asyncio
import base64
import json
import logging
import os
import ssl
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from app.mesh.auth import USER_AGENT
from app.mesh.client import ResearchClient
from app.mesh.exceptions import MeshError
from app.mesh.storage import AuthStore
from app.mesh.transport import Transport
from tools.mesh import validate_rows


def summarize(resource: str, rows: list[dict[str, Any]], start: date, end: date) -> dict[str, Any]:
    dates = []
    invalid_dates = 0
    for row in rows:
        if resource == "averages":
            break
        try:
            dates.append(date.fromisoformat(str(row.get("date", ""))[:10]))
        except ValueError:
            invalid_dates += 1
    result: dict[str, Any] = {"count": len(rows)}
    if resource in {"marks", "homework"}:
        result.update(
            {
                "earliest_date": min(dates).isoformat() if dates else None,
                "latest_date": max(dates).isoformat() if dates else None,
                "unparseable_dates": invalid_dates,
                "outside_requested_range": sum(not start <= day <= end for day in dates),
            }
        )
        id_key = "id" if resource == "marks" else "homework_entry_student_id"
        identifiers = [row.get(id_key) for row in rows]
        result["missing_ids"] = sum(identifier is None for identifier in identifiers)
        result["duplicate_ids"] = len(identifiers) - len(set(identifiers))
    if resource == "averages":
        periods = [period for row in rows for period in row.get("periods", [])]
        result.update(
            {
                "with_average": sum(row.get("average") is not None for row in rows),
                "period_count": len(periods),
                "period_fields": sorted(set().union(*(p.keys() for p in periods)))
                if periods
                else [],
                "periods_with_boundaries": sum(
                    bool(p.get("start_iso") or p.get("start"))
                    and bool(p.get("end_iso") or p.get("end"))
                    for p in periods
                ),
            }
        )
    return result


async def verify(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    narrow = date.fromisoformat(args.narrow) if args.narrow else end
    if start > end or not start <= narrow <= end:
        raise ValueError("invalid date window")
    ca_bundle = os.getenv("MESH_CA_BUNDLE") or None
    tls: ssl.SSLContext | bool = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True
    store = AuthStore(Path(os.getenv("MESH_AUTH_FILE") or ".secrets/auth.json"))
    async with httpx.AsyncClient(
        timeout=float(os.getenv("MESH_HTTP_TIMEOUT") or "20"),
        verify=tls,
        proxy=os.getenv("MESH_PROXY") or None,
        trust_env=False,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as http:
        client = ResearchClient(
            Transport(http),
            store,
            profile_id=os.getenv("MESH_PROFILE_ID") or None,
            api=args.api,
            date_params=args.date_params,
        )
        profile = await client.get_profile()
        children = profile["family"]["children"]
        report: dict[str, Any] = {
            "checked_at": datetime.now(ZoneInfo("Europe/Moscow")).isoformat(),
            "api": args.api,
            "date_params": args.date_params,
            "profile_type": client.profile_type,
            "child_count": len(children),
            "requested_range": [start.isoformat(), end.isoformat()],
            "narrow_date": narrow.isoformat(),
            "children": [],
        }
        state = store.load()
        try:
            token = state.mesh_access_token.get_secret_value() if state.mesh_access_token else ""
            segment = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            issued, expires = claims.get("iat"), claims.get("exp")
            report["mesh_token_ttl_seconds"] = (
                expires - issued if type(issued) is int and type(expires) is int else None
            )
        except (ValueError, IndexError, AttributeError):
            report["mesh_token_ttl_seconds"] = None
        for index, child in enumerate(children, 1):
            client.student_id = str(child["id"])
            client.select_student(profile)
            details: dict[str, Any] = {"child": index}
            for resource in ("marks", "averages", "homework"):
                rows = validate_rows(resource, await client.fetch(resource, start, end))
                details[resource] = summarize(resource, rows, start, end)
                if resource in {"marks", "homework"}:
                    narrow_rows = validate_rows(
                        resource, await client.fetch(resource, narrow, narrow)
                    )
                    narrow_summary = summarize(resource, narrow_rows, narrow, narrow)
                    id_key = "id" if resource == "marks" else "homework_entry_student_id"
                    expected = {
                        row[id_key]
                        for row in rows
                        if str(row.get("date", ""))[:10] == narrow.isoformat()
                    }
                    actual = {row[id_key] for row in narrow_rows}
                    narrow_summary["matches_broad_window_subset"] = actual == expected
                    details[resource + "_narrow"] = narrow_summary
            report["children"].append(details)
        problems = []
        for details in report["children"]:
            for resource in ("marks", "homework", "marks_narrow", "homework_narrow"):
                stats = details[resource]
                if (
                    any(
                        stats[key]
                        for key in (
                            "unparseable_dates",
                            "outside_requested_range",
                            "missing_ids",
                            "duplicate_ids",
                        )
                    )
                    or stats.get("matches_broad_window_subset") is False
                ):
                    problems.append({"child": details["child"], "resource": resource})
        report["validation_failures"] = problems
        report["nonempty_data_confirmed"] = bool(children) and all(
            details[resource]["count"]
            for details in report["children"]
            for resource in ("marks", "averages", "homework")
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if problems else (0 if report["nonempty_data_confirmed"] else 3)


def main() -> int:
    load_dotenv(".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--narrow", help="Single date to compare with the broad range")
    parser.add_argument("--api", choices=["mobile", "web"], default="mobile")
    parser.add_argument(
        "--date-params", choices=["from-to", "from_date-to_date"], default="from-to"
    )
    args = parser.parse_args()
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        return asyncio.run(verify(args))
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
