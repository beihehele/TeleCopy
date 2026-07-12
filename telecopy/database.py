"""SQLite-backed persistent state for TeleCopy."""

from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3

from telecopy.config import Route


logger = logging.getLogger(__name__)

_CREDENTIAL_FINGERPRINT_KEY = "credential_fingerprint"
_LEGACY_MIGRATION_KEY = "legacy_copy_map_migrated"
_ACTIVE_JOB_STATUSES = ("queued", "running")
_TERMINAL_JOB_STATUSES = ("completed", "failed", "interrupted")
_VALID_JOB_STATUSES = frozenset(
    (*_ACTIVE_JOB_STATUSES, *_TERMINAL_JOB_STATUSES)
)

_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_tasks (
        source_id INTEGER PRIMARY KEY,
        destination_id INTEGER NOT NULL,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_records (
        source_id INTEGER NOT NULL,
        destination_id INTEGER NOT NULL,
        source_message_id INTEGER NOT NULL,
        destination_message_id INTEGER NOT NULL,
        copied_at TEXT NOT NULL,
        PRIMARY KEY (source_id, destination_id, source_message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS copy_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        destination_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        requested_by INTEGER NOT NULL,
        request_chat_id INTEGER NOT NULL,
        copied_count INTEGER NOT NULL DEFAULT 0,
        error_message TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS copy_jobs_active_route
    ON copy_jobs (source_id, destination_id)
    WHERE status IN ('queued', 'running')
    """,
)


class DuplicateWatchError(ValueError):
    """Raised when a dynamic watch already exists for a source."""


class ActiveCopyJobError(ValueError):
    """Raised when a route already has a queued or running copy job."""


class LegacyMigrationError(RuntimeError):
    """Raised when a legacy copy map cannot be safely migrated."""


@dataclass(frozen=True)
class WatchTask:
    source_id: int
    destination_id: int
    created_by: int
    created_at: str


@dataclass(frozen=True)
class CopyJob:
    id: int
    source_id: int
    destination_id: int
    status: str
    requested_by: int
    request_chat_id: int
    copied_count: int
    error_message: str | None
    created_at: str
    finished_at: str | None


class StateStore:
    """Persist watches, copy records, and copy-job status in SQLite."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def reset_for_credentials(self, fingerprint: str) -> bool:
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (_CREDENTIAL_FINGERPRINT_KEY,),
            ).fetchone()
            if row is not None and row["value"] == fingerprint:
                return False

            connection.execute("DELETE FROM watch_tasks")
            connection.execute("DELETE FROM copy_records")
            connection.execute("DELETE FROM copy_jobs")
            connection.execute(
                """
                INSERT INTO metadata (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (_CREDENTIAL_FINGERPRINT_KEY, fingerprint),
            )
            return True

    def add_watch(
        self,
        source_id: int,
        destination_id: int,
        created_by: int,
    ) -> WatchTask:
        created_at = _utc_now()
        try:
            with self._write_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO watch_tasks (
                        source_id,
                        destination_id,
                        created_by,
                        created_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (source_id, destination_id, created_by, created_at),
                )
        except sqlite3.IntegrityError as error:
            if not _is_unique_constraint(error):
                raise
            raise DuplicateWatchError(
                f"A watch already exists for source {source_id}"
            ) from None
        return WatchTask(
            source_id,
            destination_id,
            created_by,
            created_at,
        )

    def remove_watch(self, source_id: int) -> bool:
        with self._write_connection() as connection:
            cursor = connection.execute(
                "DELETE FROM watch_tasks WHERE source_id = ?",
                (source_id,),
            )
            return cursor.rowcount > 0

    def list_watches(self) -> list[WatchTask]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT source_id, destination_id, created_by, created_at
                FROM watch_tasks
                ORDER BY source_id
                """
            ).fetchall()
        return [
            WatchTask(
                row["source_id"],
                row["destination_id"],
                row["created_by"],
                row["created_at"],
            )
            for row in rows
        ]

    def was_copied(
        self,
        source_id: int,
        destination_id: int,
        message_id: int,
    ) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM copy_records
                WHERE source_id = ?
                  AND destination_id = ?
                  AND source_message_id = ?
                """,
                (source_id, destination_id, message_id),
            ).fetchone()
        return row is not None

    def record_copy(
        self,
        source_id: int,
        destination_id: int,
        message_id: int,
        destination_message_id: int,
    ) -> None:
        with self._write_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_records (
                    source_id,
                    destination_id,
                    source_message_id,
                    destination_message_id,
                    copied_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (
                    source_id,
                    destination_id,
                    source_message_id
                ) DO NOTHING
                """,
                (
                    source_id,
                    destination_id,
                    message_id,
                    destination_message_id,
                    _utc_now(),
                ),
            )

    def create_copy_job(
        self,
        source_id: int,
        destination_id: int,
        requested_by: int,
        request_chat_id: int,
    ) -> CopyJob:
        created_at = _utc_now()
        try:
            with self._write_connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO copy_jobs (
                        source_id,
                        destination_id,
                        status,
                        requested_by,
                        request_chat_id,
                        copied_count,
                        created_at
                    ) VALUES (?, ?, 'queued', ?, ?, 0, ?)
                    """,
                    (
                        source_id,
                        destination_id,
                        requested_by,
                        request_chat_id,
                        created_at,
                    ),
                )
                job_id = cursor.lastrowid
        except sqlite3.IntegrityError as error:
            if not _is_unique_constraint(error):
                raise
            raise ActiveCopyJobError(
                "An active copy job already exists for route "
                f"{source_id} -> {destination_id}"
            ) from None

        if job_id is None:
            raise RuntimeError("SQLite did not return a copy job ID")
        return CopyJob(
            id=job_id,
            source_id=source_id,
            destination_id=destination_id,
            status="queued",
            requested_by=requested_by,
            request_chat_id=request_chat_id,
            copied_count=0,
            error_message=None,
            created_at=created_at,
            finished_at=None,
        )

    def update_copy_job(
        self,
        job_id: int,
        status: str,
        copied_count: int,
        error_message: str | None = None,
    ) -> None:
        if status not in _VALID_JOB_STATUSES:
            raise ValueError(f"Invalid copy job status: {status}")
        finished_at = (
            _utc_now() if status in _TERMINAL_JOB_STATUSES else None
        )
        with self._write_connection() as connection:
            connection.execute(
                """
                UPDATE copy_jobs
                SET status = ?,
                    copied_count = ?,
                    error_message = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    copied_count,
                    error_message,
                    finished_at,
                    job_id,
                ),
            )

    def increment_copy_job_progress(self, job_id: int) -> int:
        """Persist one successful history copy and return the new count."""
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE copy_jobs
                SET copied_count = copied_count + 1
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (job_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Copy job {job_id} is not active")
            row = connection.execute(
                "SELECT copied_count FROM copy_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Copy job {job_id} disappeared")
            return row["copied_count"]

    def interrupt_active_jobs(self) -> int:
        with self._write_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE copy_jobs
                SET status = 'interrupted', finished_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (_utc_now(),),
            )
            return cursor.rowcount

    def migrate_legacy_copy_map(
        self,
        path: Path,
        route: Route | None,
    ) -> int:
        migrated_path = path.with_name(f"{path.name}.migrated")
        if route is None:
            if path.exists() or migrated_path.exists():
                logger.warning(
                    "Legacy copy map was not migrated because no complete "
                    "built-in route is configured"
                )
            return 0

        with closing(self._connect()) as connection:
            marker = connection.execute(
                "SELECT 1 FROM metadata WHERE key = ?",
                (_LEGACY_MIGRATION_KEY,),
            ).fetchone()
        if marker is not None:
            return 0

        if path.exists() and migrated_path.exists():
            raise LegacyMigrationError(
                "Both legacy copy map and migrated copy map exist; "
                "migration state is ambiguous"
            )
        if migrated_path.exists():
            migration_source = migrated_path
        elif path.exists():
            migration_source = path
        else:
            return 0

        entries = _load_legacy_entries(migration_source)
        try:
            with self._write_connection() as connection:
                changes_before = connection.total_changes
                copied_at = _utc_now()
                connection.executemany(
                    """
                    INSERT INTO copy_records (
                        source_id,
                        destination_id,
                        source_message_id,
                        destination_message_id,
                        copied_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (
                        source_id,
                        destination_id,
                        source_message_id
                    ) DO NOTHING
                    """,
                    (
                        (
                            route.source_id,
                            route.destination_id,
                            source_message_id,
                            destination_message_id,
                            copied_at,
                        )
                        for source_message_id, destination_message_id in entries
                    ),
                )
                imported_count = connection.total_changes - changes_before
        except sqlite3.Error as error:
            raise LegacyMigrationError(
                f"Could not import legacy copy map {migration_source.name}: "
                f"{error}"
            ) from None

        if migration_source == path:
            try:
                path.replace(migrated_path)
            except OSError as error:
                raise LegacyMigrationError(
                    f"Could not rename legacy copy map {path.name}: {error}"
                ) from None

        try:
            with self._write_connection() as connection:
                connection.execute(
                    """
                    INSERT INTO metadata (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    (_LEGACY_MIGRATION_KEY, copied_at),
                )
        except sqlite3.Error as error:
            raise LegacyMigrationError(
                f"Could not finalize legacy copy map migration: {error}"
            ) from None
        return imported_count

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()


def _load_legacy_entries(path: Path) -> list[tuple[int, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LegacyMigrationError(
            f"Could not read legacy copy map {path.name}: {error}"
        ) from None

    if not isinstance(payload, dict):
        raise LegacyMigrationError(
            f"Legacy copy map {path.name} must contain a JSON object"
        )

    entries: list[tuple[int, int]] = []
    try:
        for source_message_id, destination_message_id in payload.items():
            if type(destination_message_id) is not int:
                raise TypeError
            entries.append((int(source_message_id), destination_message_id))
    except (TypeError, ValueError):
        raise LegacyMigrationError(
            f"Legacy copy map {path.name} contains a non-integer message ID"
        ) from None
    return entries


def _is_unique_constraint(error: sqlite3.IntegrityError) -> bool:
    return getattr(error, "sqlite_errorname", None) in {
        "SQLITE_CONSTRAINT_PRIMARYKEY",
        "SQLITE_CONSTRAINT_UNIQUE",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
