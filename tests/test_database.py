from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.mesh.models import Mark, Student, SubjectAverage
from app.repository.database import Database


@pytest.fixture
async def db(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = Database(tmp_path / "bot.db")
    await database.open()
    await database.save_students([Student(id="1", profile_id="2", name="Test")])
    yield database
    await database.close()


async def test_mark_deduplication_and_edit_preserves_first_seen(db: Database) -> None:
    student = (await db.students())[0]
    mark = Mark(
        id="7",
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        value="4",
        numeric_value=Decimal(4),
        lesson_date=date(2026, 9, 17),
    )
    first = "2026-09-18T12:00:00+03:00"
    assert await db.save_marks(student, [mark], [], first, initial=False) == 1
    mark.value = "5"
    mark.numeric_value = Decimal(5)
    assert await db.save_marks(student, [mark], [], "2026-09-18T12:30:00+03:00", False) == 0
    rows = await db.rows("SELECT * FROM marks")
    assert len(rows) == 1
    assert rows[0]["value"] == "5"
    assert rows[0]["first_seen_at"] == first


async def test_initial_bootstrap_does_not_notify(db: Database) -> None:
    student = (await db.students())[0]
    mark = Mark(
        id="1",
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        value="5",
        lesson_date=date(2026, 9, 1),
    )
    average = SubjectAverage(
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        period_id="term",
        average=Decimal("4.42"),
    )
    assert await db.save_marks(student, [mark], [average], "2026-09-18T12:00:00+03:00", True) == 0
    assert await db.state("bootstrap:1") == "done"
    assert (await db.rows("SELECT initial_import FROM marks"))[0]["initial_import"] == 1
    assert len(await db.rows("SELECT * FROM subject_snapshots")) == 1


async def test_snapshots_store_only_changes_and_compact_on_open(db: Database) -> None:
    student = (await db.students())[0]
    average = SubjectAverage(
        student_id="1",
        subject_id="3",
        subject_name="Математика",
        period_id="term",
        average=Decimal("4.42"),
    )
    await db.save_marks(student, [], [average], "2026-09-18T12:00:00+03:00", False)
    await db.save_marks(student, [], [average], "2026-09-18T12:30:00+03:00", False)
    average.average = Decimal("4.50")
    await db.save_marks(student, [], [average], "2026-09-18T13:00:00+03:00", False)
    query = "SELECT snapshot_at, average FROM subject_snapshots ORDER BY snapshot_at"
    assert [row["average"] for row in await db.rows(query)] == ["4.42", "4.50"]

    # Databases written by earlier versions held a snapshot for every sync.
    async with db.lock:
        for at, value in (("12:10", "4.42"), ("13:10", "4.50"), ("13:20", "4.42")):
            await db.db.execute(
                "INSERT INTO subject_snapshots(student_id,subject_id,subject_name,snapshot_at,"
                "average,period_id) VALUES('1','3','Математика',?,?,'term')",
                (f"2026-09-18T{at}:00+03:00", value),
            )
        await db.db.commit()
    await db.close()
    await db.open()
    assert [(row["snapshot_at"][11:16], row["average"]) for row in await db.rows(query)] == [
        ("12:00", "4.42"),
        ("13:00", "4.50"),
        ("13:20", "4.42"),
    ]


async def test_backup_restores_after_reopen(db: Database, tmp_path: Path) -> None:
    backup_path = tmp_path / "backup.db"
    await db.backup(backup_path)
    restored = Database(backup_path)
    await restored.open()
    assert len(await restored.students()) == 1
    await restored.close()


async def test_version_one_upgrade_preserves_family_and_adds_weekly_baselines(db: Database) -> None:
    async with db.lock:
        await db.db.execute("DROP TABLE weekly_averages")
        await db.db.execute("PRAGMA user_version=1")
        await db.db.commit()
    await db.close()
    await db.open()
    assert (await db.students())[0].id == "1"
    assert (await db.rows("PRAGMA user_version"))[0]["user_version"] == 2
    assert await db.rows("SELECT * FROM weekly_averages") == []
