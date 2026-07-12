"""Thread-safe effective route snapshots."""

from collections.abc import Iterable, Mapping
from threading import RLock
from types import MappingProxyType

from telecopy.config import Route
from telecopy.database import WatchTask


class RouteRegistry:
    """Combine a built-in route with replaceable dynamic watch tasks."""

    def __init__(self, builtin: Route | None):
        self._builtin = builtin
        self._lock = RLock()
        self._destinations: Mapping[int, tuple[int, ...]] = MappingProxyType(
            {}
        )
        self._dynamic_tasks: tuple[WatchTask, ...] = ()
        self.replace_dynamic([])

    def replace_dynamic(self, tasks: Iterable[WatchTask]) -> None:
        """Atomically replace dynamic tasks and publish effective routes."""
        dynamic_tasks = tuple(tasks)
        destinations: dict[int, set[int]] = {}
        if self._builtin is not None:
            destinations.setdefault(self._builtin.source_id, set()).add(
                self._builtin.destination_id
            )
        for task in dynamic_tasks:
            destinations.setdefault(task.source_id, set()).add(
                task.destination_id
            )

        snapshot = MappingProxyType(
            {
                source_id: tuple(sorted(destination_ids))
                for source_id, destination_ids in destinations.items()
            }
        )
        with self._lock:
            self._dynamic_tasks = dynamic_tasks
            self._destinations = snapshot

    def destinations_for(self, source_id: int) -> tuple[int, ...]:
        """Return the stable destination snapshot for a source."""
        with self._lock:
            return self._destinations.get(source_id, ())

    def contains(self, route: Route) -> bool:
        """Return whether the effective snapshot contains a route."""
        with self._lock:
            return route.destination_id in self._destinations.get(
                route.source_id, ()
            )

    @property
    def dynamic_tasks(self) -> tuple[WatchTask, ...]:
        """Return only dynamic tasks as an immutable snapshot."""
        with self._lock:
            return self._dynamic_tasks
