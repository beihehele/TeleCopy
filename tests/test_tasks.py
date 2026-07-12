from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from telecopy.config import Route
from telecopy.database import WatchTask
from telecopy.tasks import RouteRegistry


def watch(source_id, destination_id, created_by=123, created_at="now"):
    return WatchTask(source_id, destination_id, created_by, created_at)


def test_registry_accepts_empty_builtin_and_dynamic_routes():
    registry = RouteRegistry(None)

    registry.replace_dynamic([])

    assert registry.destinations_for(-1001) == ()
    assert registry.contains(Route(-1001, -2001)) is False
    assert registry.dynamic_tasks == ()


def test_registry_deduplicates_same_builtin_and_dynamic_route():
    registry = RouteRegistry(Route(-1001, -2001))

    registry.replace_dynamic([watch(-1001, -2001)])

    assert registry.destinations_for(-1001) == (-2001,)
    assert registry.contains(Route(-1001, -2001)) is True


def test_registry_keeps_distinct_destinations_in_stable_order():
    registry = RouteRegistry(Route(-1001, -2001))

    registry.replace_dynamic(
        [
            watch(-1001, -2003),
            watch(-1001, -2002),
            watch(-1001, -2003),
        ]
    )

    assert registry.destinations_for(-1001) == (-2003, -2002, -2001)
    assert registry.contains(Route(-1001, -2002)) is True
    assert registry.contains(Route(-1001, -9999)) is False


def test_dynamic_snapshot_excludes_builtin_and_is_immutable():
    builtin = Route(-1001, -2001)
    dynamic = [watch(-1002, -2002)]
    registry = RouteRegistry(builtin)

    registry.replace_dynamic(dynamic)
    snapshot = registry.dynamic_tasks
    dynamic.append(watch(-1003, -2003))

    assert snapshot == (watch(-1002, -2002),)
    assert registry.dynamic_tasks == snapshot
    assert all(
        (task.source_id, task.destination_id)
        != (builtin.source_id, builtin.destination_id)
        for task in registry.dynamic_tasks
    )
    with pytest.raises(TypeError):
        snapshot[0] = watch(-1004, -2004)


def test_replace_dynamic_builds_without_blocking_snapshot_reads():
    old_tasks = [watch(-1001, -2001), watch(-1001, -2002)]
    new_tasks = [watch(-1001, -3001), watch(-1001, -3002)]
    registry = RouteRegistry(None)
    registry.replace_dynamic(old_tasks)
    building_started = Event()
    continue_building = Event()

    def blocking_tasks():
        building_started.set()
        assert continue_building.wait(timeout=2)
        yield from new_tasks

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(registry.replace_dynamic, blocking_tasks())
        assert building_started.wait(timeout=1)
        reader = executor.submit(registry.destinations_for, -1001)
        try:
            snapshot_during_build = reader.result(timeout=0.5)
        finally:
            continue_building.set()
            writer.result(timeout=2)

    assert snapshot_during_build == (-2002, -2001)
    assert registry.destinations_for(-1001) == (-3002, -3001)
