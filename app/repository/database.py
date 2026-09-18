"""One async SQLite connection, transaction lock, durable bootstrap and safe backups."""

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

from app.mesh.models import Homework, Mark, Student, SubjectAverage

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
 id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, person_id TEXT, name TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS marks (
 student_id TEXT NOT NULL REFERENCES students(id), mesh_mark_id TEXT NOT NULL,
 subject_id TEXT NOT NULL, subject_name TEXT NOT NULL, value TEXT NOT NULL,
 numeric_value TEXT, weight INTEGER NOT NULL, work_type TEXT NOT NULL,
 comment TEXT NOT NULL, lesson_date TEXT NOT NULL, created_at_mesh TEXT,
 first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL, initial_import INTEGER NOT NULL,
 PRIMARY KEY(student_id, mesh_mark_id)
);
CREATE INDEX IF NOT EXISTS marks_seen ON marks(first_seen_at);
CREATE INDEX IF NOT EXISTS marks_date ON marks(student_id, lesson_date);
CREATE TABLE IF NOT EXISTS subject_snapshots (
 id INTEGER PRIMARY KEY, student_id TEXT NOT NULL REFERENCES students(id),
 subject_id TEXT NOT NULL, subject_name TEXT NOT NULL, snapshot_at TEXT NOT NULL,
 average TEXT, period_id TEXT NOT NULL, period_start TEXT, period_end TEXT,
 UNIQUE(student_id, subject_id, snapshot_at)
);
CREATE INDEX IF NOT EXISTS snapshots_lookup
 ON subject_snapshots(student_id, subject_id, period_id, snapshot_at);
CREATE TABLE IF NOT EXISTS homework (
 student_id TEXT NOT NULL REFERENCES students(id), mesh_homework_id TEXT NOT NULL,
 subject_id TEXT NOT NULL, subject_name TEXT NOT NULL, lesson_date TEXT NOT NULL,
 text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(student_id, mesh_homework_id)
);
CREATE INDEX IF NOT EXISTS homework_date ON homework(student_id, lesson_date);
CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS report_delivery (
 key TEXT PRIMARY KEY, content TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0,
 complete INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS weekly_averages (
 student_id TEXT NOT NULL, subject_id TEXT NOT NULL, week_start TEXT NOT NULL,
 period_id TEXT NOT NULL, average TEXT, snapshot_at TEXT NOT NULL,
 PRIMARY KEY(student_id,subject_id,week_start)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.connection: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self.connection is None:
            raise RuntimeError("database not opened")
        return self.connection

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = await aiosqlite.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.path.chmod(0o600)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self.db.execute("PRAGMA busy_timeout=10000")
        version = (await self.rows("PRAGMA user_version"))[0]["user_version"]
        if version not in {0, 1, 2}:
            raise RuntimeError("unsupported database schema version")
        await self.db.executescript(SCHEMA)
        await self.db.execute("PRAGMA user_version=2")
        await self.db.commit()

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()
            self.connection = None

    async def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.lock:
            async with self.db.execute(sql, params) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def state(self, key: str) -> str | None:
        rows = await self.rows("SELECT value FROM sync_state WHERE key=?", (key,))
        return str(rows[0]["value"]) if rows else None

    async def _state(self, key: str, value: str, now: str) -> None:
        await self.db.execute(
            "INSERT INTO sync_state VALUES(?,?,?) ON CONFLICT(key) "
            "DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, now),
        )

    async def set_state(self, key: str, value: str, now: str) -> None:
        async with self.lock:
            await self._state(key, value, now)
            await self.db.commit()

    async def students(self) -> list[Student]:
        return [
            Student(
                id=row["id"],
                profile_id=row["profile_id"],
                person_id=row["person_id"],
                name=row["name"],
            )
            for row in await self.rows("SELECT * FROM students WHERE active=1 ORDER BY rowid")
        ]

    async def save_students(self, students: list[Student]) -> None:
        if not students:
            raise RuntimeError("empty family profile")
        async with self.lock:
            try:
                await self.db.execute("UPDATE students SET active=0")
                for student in students:
                    await self.db.execute(
                        "INSERT INTO students VALUES(?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET "
                        "profile_id=excluded.profile_id,person_id=excluded.person_id,"
                        "name=excluded.name,active=1",
                        (student.id, student.profile_id, student.person_id, student.name),
                    )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def save_marks(
        self,
        student: Student,
        marks: list[Mark],
        averages: list[SubjectAverage],
        now: str,
        initial: bool,
    ) -> int:
        new = 0
        async with self.lock:
            try:
                for mark in marks:
                    values = (
                        student.id,
                        mark.id,
                        mark.subject_id,
                        mark.subject_name,
                        mark.value,
                        str(mark.numeric_value) if mark.numeric_value is not None else None,
                        mark.weight,
                        mark.work_type,
                        mark.comment,
                        mark.lesson_date.isoformat(),
                        mark.created_at_mesh,
                        now,
                        now,
                        int(initial),
                    )
                    async with self.db.execute(
                        "INSERT INTO marks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(student_id,mesh_mark_id) DO NOTHING",
                        values,
                    ) as cursor:
                        new += cursor.rowcount
                    await self.db.execute(
                        "UPDATE marks SET subject_id=?,subject_name=?,value=?,numeric_value=?,"
                        "weight=?,work_type=?,comment=?,lesson_date=?,"
                        "created_at_mesh=?,updated_at=? "
                        "WHERE student_id=? AND mesh_mark_id=?",
                        (*values[2:11], now, student.id, mark.id),
                    )
                for average in averages:
                    await self.db.execute(
                        "INSERT INTO subject_snapshots(student_id,subject_id,subject_name,"
                        "snapshot_at,average,period_id,period_start,period_end) "
                        "VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(student_id,subject_id,snapshot_at) DO NOTHING",
                        (
                            student.id,
                            average.subject_id,
                            average.subject_name,
                            now,
                            str(average.average) if average.average is not None else None,
                            average.period_id,
                            average.period_start.isoformat() if average.period_start else None,
                            average.period_end.isoformat() if average.period_end else None,
                        ),
                    )
                await self._state(f"bootstrap:{student.id}", "done", now)
                await self._state(f"marks:{student.id}", now, now)
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return 0 if initial else new

    async def save_homework(
        self,
        student: Student,
        items: list[Homework],
        start: str,
        end: str,
        now: str,
    ) -> None:
        async with self.lock:
            try:
                # Only a successful, complete range response can replace that cached range.
                for item in items:
                    await self.db.execute(
                        "INSERT INTO homework VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(student_id,mesh_homework_id) DO UPDATE SET "
                        "subject_id=excluded.subject_id,subject_name=excluded.subject_name,"
                        "lesson_date=excluded.lesson_date,text=excluded.text,updated_at=excluded.updated_at",
                        (
                            student.id,
                            item.id,
                            item.subject_id,
                            item.subject_name,
                            item.lesson_date.isoformat(),
                            item.text,
                            now,
                            now,
                        ),
                    )
                ids = [item.id for item in items]
                placeholders = ",".join("?" for _ in ids)
                await self.db.execute(
                    "DELETE FROM homework WHERE student_id=? AND lesson_date BETWEEN ? AND ?"
                    + (f" AND mesh_homework_id NOT IN ({placeholders})" if ids else ""),
                    (student.id, start, end, *ids),
                )
                await self._state(f"homework:{student.id}", now, now)
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def prepare_delivery(self, key: str, parts: list[str], now: str) -> dict[str, Any]:
        async with self.lock:
            await self.db.execute(
                "INSERT OR IGNORE INTO report_delivery(key,content,created_at) VALUES(?,?,?)",
                (key, json.dumps(parts, ensure_ascii=False), now),
            )
            await self.db.commit()
        return (await self.rows("SELECT * FROM report_delivery WHERE key=?", (key,)))[0]

    async def delivery_progress(self, key: str, next_part: int, complete: bool, now: str) -> None:
        async with self.lock:
            await self.db.execute(
                "UPDATE report_delivery SET next_part=?,complete=?,completed_at=? WHERE key=?",
                (next_part, int(complete), now if complete else None, key),
            )
            await self.db.commit()

    async def backup(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self.lock:
            async with aiosqlite.connect(target) as destination:
                await self.db.backup(destination)
        await asyncio.to_thread(target.chmod, 0o600)
