from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import CaseRecord, CaseStatus, VoteDecision, VoteRecord


def _parse_sqlite_url(url: str) -> Path:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError("Only sqlite URLS like sqlite:///path/to.db are supported.")
    path = url[len(prefix) :]
    db_path = Path(path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


class Storage:
    """
    Lightweight SQLite helper around aiosqlite.
    """

    def __init__(self, url: str):
        self.db_path = _parse_sqlite_url(url)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if not self._conn:
            raise RuntimeError("Storage not initialized, call connect() first.")
        return self._conn

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON;")
        await self._create_schema()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_schema(self) -> None:
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                settings TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                offender_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closes_at TEXT NOT NULL,
                poll_chat_id INTEGER,
                poll_message_id INTEGER,
                config_snapshot TEXT NOT NULL,
                participant_target INTEGER NOT NULL,
                UNIQUE(chat_id, message_id)
            );

            CREATE TABLE IF NOT EXISTS votes (
                case_id INTEGER NOT NULL,
                voter_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (case_id, voter_id),
                FOREIGN KEY (case_id) REFERENCES cases(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS blacklist (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT,
                added_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );
            """
        )
        await self.conn.commit()

    async def upsert_chat(self, chat_id: int, title: str) -> None:
        ts = datetime.now(tz=timezone.utc).isoformat()
        await self.conn.execute(
            """
            INSERT INTO chats(chat_id, title, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title=excluded.title,
                updated_at=excluded.updated_at;
            """,
            (chat_id, title, ts),
        )
        await self.conn.commit()

    async def list_chats(self) -> list[tuple[int, str]]:
        cur = await self.conn.execute(
            "SELECT chat_id, COALESCE(title, '') AS title FROM chats ORDER BY updated_at DESC;"
        )
        rows = await cur.fetchall()
        return [(row["chat_id"], row["title"]) for row in rows]

    async def get_chat_settings(self, chat_id: int) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            "SELECT settings FROM chats WHERE chat_id = ?;", (chat_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return json.loads(row["settings"])

    async def get_chat_title(self, chat_id: int) -> str | None:
        cur = await self.conn.execute(
            "SELECT title FROM chats WHERE chat_id = ?;", (chat_id,)
        )
        row = await cur.fetchone()
        if row:
            return row["title"]
        return None

    async def set_chat_settings(self, chat_id: int, settings: dict[str, Any]) -> None:
        await self.conn.execute(
            """
            INSERT INTO chats(chat_id, settings, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                settings=excluded.settings,
                updated_at=excluded.updated_at;
            """,
            (
                chat_id,
                json.dumps(settings),
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        await self.conn.commit()

    async def create_case(
        self,
        chat_id: int,
        message_id: int,
        offender_id: int,
        reporter_id: int,
        closes_at: datetime,
        config_snapshot: dict[str, Any],
        participant_target: int,
    ) -> CaseRecord:
        opened_at = datetime.now(tz=timezone.utc)
        cur = await self.conn.execute(
            """
            INSERT INTO cases(chat_id, message_id, offender_id, reporter_id, status,
                              opened_at, closes_at, config_snapshot, participant_target)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                chat_id,
                message_id,
                offender_id,
                reporter_id,
                CaseStatus.OPEN.value,
                opened_at.isoformat(),
                closes_at.isoformat(),
                json.dumps(config_snapshot),
                participant_target,
            ),
        )
        await self.conn.commit()
        case_id = cur.lastrowid
        return CaseRecord(
            id=case_id,
            chat_id=chat_id,
            message_id=message_id,
            offender_id=offender_id,
            reporter_id=reporter_id,
            status=CaseStatus.OPEN,
            opened_at=opened_at,
            closes_at=closes_at,
            poll_chat_id=None,
            poll_message_id=None,
            config_snapshot=config_snapshot,
            participant_target=participant_target,
        )

    async def get_case_by_message(
        self, chat_id: int, message_id: int
    ) -> CaseRecord | None:
        cur = await self.conn.execute(
            """
            SELECT * FROM cases WHERE chat_id = ? AND message_id = ?;
            """,
            (chat_id, message_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return self._row_to_case(row)

    async def get_case(self, case_id: int) -> CaseRecord | None:
        cur = await self.conn.execute("SELECT * FROM cases WHERE id = ?;", (case_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return self._row_to_case(row)

    async def update_case_poll(
        self, case_id: int, poll_chat_id: int, poll_message_id: int
    ) -> None:
        await self.conn.execute(
            """
            UPDATE cases
            SET poll_chat_id = ?, poll_message_id = ?
            WHERE id = ?;
            """,
            (poll_chat_id, poll_message_id, case_id),
        )
        await self.conn.commit()

    async def set_case_status(self, case_id: int, status: CaseStatus) -> None:
        await self.conn.execute(
            "UPDATE cases SET status = ? WHERE id = ?;",
            (status.value, case_id),
        )
        await self.conn.commit()

    async def record_vote(
        self, case_id: int, voter_id: int, decision: VoteDecision
    ) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        await self.conn.execute(
            """
            INSERT INTO votes(case_id, voter_id, decision, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(case_id, voter_id) DO UPDATE SET
                decision=excluded.decision,
                updated_at=excluded.updated_at;
            """,
            (case_id, voter_id, decision.value, now),
        )
        await self.conn.commit()

    async def retract_vote(self, case_id: int, voter_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM votes WHERE case_id = ? AND voter_id = ?;",
            (case_id, voter_id),
        )
        await self.conn.commit()

    async def get_votes(self, case_id: int) -> list[VoteRecord]:
        cur = await self.conn.execute(
            "SELECT * FROM votes WHERE case_id = ?;", (case_id,)
        )
        rows = await cur.fetchall()
        return [
            VoteRecord(
                case_id=row["case_id"],
                voter_id=row["voter_id"],
                decision=VoteDecision(row["decision"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    async def list_cases(
        self, chat_id: int, limit: int = 10
    ) -> list[CaseRecord]:
        cur = await self.conn.execute(
            """
            SELECT * FROM cases
            WHERE chat_id = ?
            ORDER BY opened_at DESC
            LIMIT ?;
            """,
            (chat_id, limit),
        )
        rows = await cur.fetchall()
        return [self._row_to_case(row) for row in rows]

    async def list_open_cases(self) -> list[CaseRecord]:
        cur = await self.conn.execute(
            "SELECT * FROM cases WHERE status = ?;",
            (CaseStatus.OPEN.value,),
        )
        rows = await cur.fetchall()
        return [self._row_to_case(row) for row in rows]

    async def count_recent_reports(
        self, chat_id: int, reporter_id: int, since: datetime
    ) -> int:
        cur = await self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM cases
            WHERE chat_id = ?
              AND reporter_id = ?
              AND opened_at >= ?
            """,
            (chat_id, reporter_id, since.isoformat()),
        )
        row = await cur.fetchone()
        return row["c"] if row else 0

    async def blacklist_add(
        self, chat_id: int, user_id: int, reason: str | None
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO blacklist(chat_id, user_id, reason, added_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                reason=excluded.reason,
                added_at=excluded.added_at;
            """,
            (
                chat_id,
                user_id,
                reason,
                datetime.now(tz=timezone.utc).isoformat(),
            ),
        )
        await self.conn.commit()

    async def blacklist_remove(self, chat_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM blacklist WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id),
        )
        await self.conn.commit()

    async def blacklist_contains(self, chat_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM blacklist WHERE chat_id = ? AND user_id = ?;",
            (chat_id, user_id),
        )
        return await cur.fetchone() is not None

    def _row_to_case(self, row: aiosqlite.Row) -> CaseRecord:
        return CaseRecord(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            offender_id=row["offender_id"],
            reporter_id=row["reporter_id"],
            status=CaseStatus(row["status"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closes_at=datetime.fromisoformat(row["closes_at"]),
            poll_chat_id=row["poll_chat_id"],
            poll_message_id=row["poll_message_id"],
            config_snapshot=json.loads(row["config_snapshot"]),
            participant_target=row["participant_target"],
        )
