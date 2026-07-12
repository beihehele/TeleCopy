from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from telecopy.config import Route
from telecopy.database import (
    ActiveCopyJobError,
    DuplicateWatchError,
    LegacyMigrationError,
    StateStore,
    WatchTask,
)


@pytest.fixture
def database_path(tmp_path):
    return tmp_path / "state" / "telecopy.db"


@pytest.fixture
def store(database_path):
    state_store = StateStore(database_path)
    state_store.initialize()
    return state_store


def test_initialize_creates_complete_schema_and_enables_wal(database_path):
    store = StateStore(database_path)

    store.initialize()

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert {"metadata", "watch_tasks", "copy_records", "copy_jobs"} <= tables
    assert journal_mode == "wal"


def test_watch_crud_is_persistent_and_sorted(database_path):
    store = StateStore(database_path)
    store.initialize()
    later = store.add_watch(-1001, -2001, created_by=123)
    earlier = store.add_watch(-3001, -4001, created_by=456)

    reopened = StateStore(database_path)

    assert reopened.list_watches() == [earlier, later]
    assert reopened.remove_watch(-3001) is True
    assert reopened.remove_watch(-3001) is False
    assert reopened.list_watches() == [later]


def test_watch_source_is_unique(store):
    store.add_watch(-1001, -2001, created_by=123)

    with pytest.raises(DuplicateWatchError, match="-1001"):
        store.add_watch(-1001, -2002, created_by=123)


def test_add_watch_preserves_non_unique_integrity_errors(store):
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        store.add_watch(-1001, None, created_by=123)


def test_copy_records_are_unique_per_route(store):
    store.record_copy(-1001, -2001, 10, 20)

    assert store.was_copied(-1001, -2001, 10) is True
    assert store.was_copied(-1001, -2002, 10) is False
    assert store.was_copied(-1001, -2001, 11) is False

    store.record_copy(-1001, -2001, 10, 21)
    with sqlite3.connect(store.database_path) as connection:
        destination_message_id = connection.execute(
            """
            SELECT destination_message_id
            FROM copy_records
            WHERE source_id = ? AND destination_id = ?
              AND source_message_id = ?
            """,
            (-1001, -2001, 10),
        ).fetchone()[0]
    assert destination_message_id == 20


def test_record_copy_does_not_ignore_non_primary_key_constraint_errors(store):
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        store.record_copy(-1001, -2001, 10, None)


def test_reset_for_credentials_handles_first_same_and_changed_fingerprint(store):
    assert store.reset_for_credentials("first") is True
    store.add_watch(-1001, -2001, created_by=123)
    store.record_copy(-1001, -2001, 10, 20)
    job = store.create_copy_job(-1001, -2001, 123, 456)

    assert store.reset_for_credentials("first") is False
    assert len(store.list_watches()) == 1
    assert store.was_copied(-1001, -2001, 10) is True

    assert store.reset_for_credentials("changed") is True
    assert store.list_watches() == []
    assert store.was_copied(-1001, -2001, 10) is False
    with sqlite3.connect(store.database_path) as connection:
        job_count = connection.execute(
            "SELECT COUNT(*) FROM copy_jobs WHERE id = ?", (job.id,)
        ).fetchone()[0]
        schema_count = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name = 'watch_tasks'
            """
        ).fetchone()[0]
    assert job_count == 0
    assert schema_count == 1


def test_copy_job_lifecycle_and_frozen_models(store):
    job = store.create_copy_job(-1001, -2001, 123, 456)

    assert job.status == "queued"
    assert job.copied_count == 0
    assert job.finished_at is None
    store.update_copy_job(job.id, "running", 2)
    store.update_copy_job(job.id, "completed", 3)

    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            """
            SELECT status, copied_count, error_message, finished_at
            FROM copy_jobs WHERE id = ?
            """,
            (job.id,),
        ).fetchone()
    assert row[0:3] == ("completed", 3, None)
    assert row[3] is not None
    with pytest.raises(FrozenInstanceError):
        job.status = "running"
    with pytest.raises(FrozenInstanceError):
        WatchTask(-1, -2, 3, "now").source_id = -4


def test_increment_copy_job_progress_persists_each_success(store):
    job = store.create_copy_job(-1001, -2001, 123, 456)
    store.update_copy_job(job.id, "running", 0)

    assert store.increment_copy_job_progress(job.id) == 1
    assert store.increment_copy_job_progress(job.id) == 2

    with sqlite3.connect(store.database_path) as connection:
        copied_count = connection.execute(
            "SELECT copied_count FROM copy_jobs WHERE id = ?",
            (job.id,),
        ).fetchone()[0]
    assert copied_count == 2


def test_same_route_has_only_one_active_copy_job(store):
    first = store.create_copy_job(-1001, -2001, 123, 456)

    with pytest.raises(ActiveCopyJobError, match="-1001.*-2001"):
        store.create_copy_job(-1001, -2001, 999, 888)

    store.update_copy_job(first.id, "completed", 5)
    second = store.create_copy_job(-1001, -2001, 999, 888)
    assert second.id != first.id


def test_create_copy_job_preserves_non_unique_integrity_errors(store):
    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        store.create_copy_job(-1001, -2001, None, 456)


def test_active_job_unique_index_resolves_two_connection_race(database_path):
    first_store = StateStore(database_path)
    second_store = StateStore(database_path)
    first_store.initialize()
    barrier = threading.Barrier(2)

    def create_job(state_store):
        barrier.wait()
        try:
            state_store.create_copy_job(-1001, -2001, 123, 456)
            return "created"
        except ActiveCopyJobError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(create_job, (first_store, second_store))
        )

    assert sorted(outcomes) == ["created", "duplicate"]
    with sqlite3.connect(database_path) as connection:
        active_count = connection.execute(
            """
            SELECT COUNT(*) FROM copy_jobs
            WHERE source_id = ? AND destination_id = ?
              AND status IN ('queued', 'running')
            """,
            (-1001, -2001),
        ).fetchone()[0]
    assert active_count == 1


def test_interrupt_active_jobs_marks_queued_and_running_as_interrupted(store):
    queued = store.create_copy_job(-1001, -2001, 123, 456)
    running = store.create_copy_job(-1002, -2002, 123, 456)
    completed = store.create_copy_job(-1003, -2003, 123, 456)
    store.update_copy_job(running.id, "running", 2)
    store.update_copy_job(completed.id, "completed", 3)

    assert store.interrupt_active_jobs() == 2

    with sqlite3.connect(store.database_path) as connection:
        rows = dict(
            connection.execute("SELECT id, status FROM copy_jobs ORDER BY id")
        )
    assert rows[queued.id] == "interrupted"
    assert rows[running.id] == "interrupted"
    assert rows[completed.id] == "completed"


def test_migrate_legacy_copy_map_requires_route(store, tmp_path):
    legacy_path = tmp_path / "copy_map.json"
    legacy_path.write_text(json.dumps({"10": 20}), encoding="utf-8")

    assert store.migrate_legacy_copy_map(legacy_path, None) == 0

    assert legacy_path.exists()
    assert not legacy_path.with_name("copy_map.json.migrated").exists()
    assert store.was_copied(-1001, -2001, 10) is False


def test_migrate_legacy_copy_map_imports_once_and_renames(store, tmp_path):
    legacy_path = tmp_path / "copy_map.json"
    legacy_path.write_text(
        json.dumps({"10": 20, "11": 21}),
        encoding="utf-8",
    )

    count = store.migrate_legacy_copy_map(
        legacy_path, Route(-1001, -2001)
    )

    assert count == 2
    assert not legacy_path.exists()
    assert legacy_path.with_name("copy_map.json.migrated").exists()
    assert store.was_copied(-1001, -2001, 10) is True
    assert store.was_copied(-1001, -2001, 11) is True

    legacy_path.write_text(json.dumps({"12": 22}), encoding="utf-8")
    assert (
        store.migrate_legacy_copy_map(
            legacy_path, Route(-1001, -2001)
        )
        == 0
    )
    assert legacy_path.exists()
    assert store.was_copied(-1001, -2001, 12) is False


def test_migration_commits_import_before_rename(
    store, tmp_path, monkeypatch
):
    legacy_path = tmp_path / "copy_map.json"
    legacy_path.write_text(json.dumps({"10": 20}), encoding="utf-8")
    original_replace = Path.replace

    def fail_legacy_rename(path, target):
        if path == legacy_path:
            raise OSError("simulated rename failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_legacy_rename)

    with pytest.raises(LegacyMigrationError, match="rename"):
        store.migrate_legacy_copy_map(
            legacy_path, Route(-1001, -2001)
        )

    assert legacy_path.exists()
    assert store.was_copied(-1001, -2001, 10) is True


def test_migration_recovers_renamed_file_without_marker(store, tmp_path):
    legacy_path = tmp_path / "copy_map.json"
    migrated_path = tmp_path / "copy_map.json.migrated"
    migrated_path.write_text(json.dumps({"10": 20}), encoding="utf-8")
    store.record_copy(-1001, -2001, 10, 20)

    assert (
        store.migrate_legacy_copy_map(
            legacy_path, Route(-1001, -2001)
        )
        == 0
    )

    assert migrated_path.exists()
    with sqlite3.connect(store.database_path) as connection:
        marker_count = connection.execute(
            """
            SELECT COUNT(*) FROM metadata
            WHERE key = 'legacy_copy_map_migrated'
            """
        ).fetchone()[0]
        record_count = connection.execute(
            "SELECT COUNT(*) FROM copy_records"
        ).fetchone()[0]
    assert marker_count == 1
    assert record_count == 1


def test_migration_recovers_after_marker_write_failure(store, tmp_path):
    legacy_path = tmp_path / "copy_map.json"
    migrated_path = tmp_path / "copy_map.json.migrated"
    legacy_path.write_text(json.dumps({"10": 20}), encoding="utf-8")
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_legacy_marker
            BEFORE INSERT ON metadata
            WHEN NEW.key = 'legacy_copy_map_migrated'
            BEGIN
                SELECT RAISE(FAIL, 'simulated marker failure');
            END
            """
        )

    with pytest.raises(LegacyMigrationError, match="finalize"):
        store.migrate_legacy_copy_map(
            legacy_path, Route(-1001, -2001)
        )

    assert not legacy_path.exists()
    assert migrated_path.exists()
    assert store.was_copied(-1001, -2001, 10) is True
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TRIGGER fail_legacy_marker")

    assert (
        store.migrate_legacy_copy_map(
            legacy_path, Route(-1001, -2001)
        )
        == 0
    )
    with sqlite3.connect(store.database_path) as connection:
        marker_count = connection.execute(
            """
            SELECT COUNT(*) FROM metadata
            WHERE key = 'legacy_copy_map_migrated'
            """
        ).fetchone()[0]
        record_count = connection.execute(
            "SELECT COUNT(*) FROM copy_records"
        ).fetchone()[0]
    assert marker_count == 1
    assert record_count == 1


def test_bad_legacy_json_leaves_database_and_file_unchanged(store, tmp_path):
    store.record_copy(-1001, -2001, 9, 19)
    legacy_path = tmp_path / "copy_map.json"
    legacy_path.write_text("{bad json", encoding="utf-8")

    with pytest.raises(LegacyMigrationError, match="copy_map.json"):
        store.migrate_legacy_copy_map(
            legacy_path, Route(-1001, -2001)
        )

    assert legacy_path.exists()
    assert not legacy_path.with_name("copy_map.json.migrated").exists()
    assert store.was_copied(-1001, -2001, 9) is True
    with sqlite3.connect(store.database_path) as connection:
        record_count = connection.execute(
            "SELECT COUNT(*) FROM copy_records"
        ).fetchone()[0]
        marker_count = connection.execute(
            """
            SELECT COUNT(*) FROM metadata
            WHERE key = 'legacy_copy_map_migrated'
            """
        ).fetchone()[0]
    assert record_count == 1
    assert marker_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"not-an-integer": 20}',
        '{"10": "not-an-integer"}',
        '{"10": "20"}',
        '{"10": 20.5}',
        '{"10": true}',
    ],
)
def test_invalid_legacy_shape_has_clear_error_without_db_changes(
    store, tmp_path, payload
):
    legacy_path = tmp_path / "copy_map.json"
    legacy_path.write_text(payload, encoding="utf-8")

    with pytest.raises(LegacyMigrationError):
        store.migrate_legacy_copy_map(
            legacy_path, Route(-1001, -2001)
        )

    assert legacy_path.exists()
    with sqlite3.connect(store.database_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM copy_records"
            ).fetchone()[0]
            == 0
        )
