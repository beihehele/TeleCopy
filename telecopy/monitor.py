"""Lightweight TDLib update dispatcher for real-time forwarding."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock, Timer

from telecopy.config import Route
from telecopy.copy_service import CopyService
from telecopy.tasks import RouteRegistry
from telecopy.tdlib_client import EXCLUDE_TYPES, NEW_MESSAGE_UPDATE

DEFAULT_ALBUM_WAIT_SECONDS = 1.0
TimerFactory = Callable[..., Timer]


class MonitorDispatcher:
    """Register one TDLib handler and enqueue real-time forwarding work."""

    def __init__(
        self,
        client,
        copy_service: CopyService,
        registry: RouteRegistry,
        builtin_route: Route | None,
        *,
        album_wait_seconds: float = DEFAULT_ALBUM_WAIT_SECONDS,
        timer_factory: TimerFactory = Timer,
    ) -> None:
        if album_wait_seconds <= 0:
            raise ValueError("album_wait_seconds must be positive")
        self._client = client
        self._copy_service = copy_service
        self._registry = registry
        self._builtin_route = builtin_route
        self._album_wait_seconds = album_wait_seconds
        self._timer_factory = timer_factory
        self._handler = None
        self._lock = Lock()
        self._album_buffers: dict[tuple[int, int], set[int]] = {}
        self._album_timers: dict[tuple[int, int], Timer] = {}

    def start(self) -> None:
        if self._handler is not None:
            return
        self._handler = self.handle_update
        self._client.add_new_message_handler(self._handler)

    def stop(self) -> None:
        if self._handler is not None:
            self._client.remove_new_message_handler(self._handler)
            self._handler = None
        self.flush_pending_albums()

    def flush_pending_albums(self) -> None:
        """Flush every buffered album immediately."""
        with self._lock:
            keys = list(self._album_buffers)
            for key in keys:
                self._cancel_timer_locked(key)
            pending = {
                key: sorted(message_ids)
                for key, message_ids in self._album_buffers.items()
            }
            self._album_buffers.clear()
        for (source_id, _album_id), message_ids in pending.items():
            if message_ids:
                self._enqueue_to_destinations(source_id, tuple(message_ids))

    def handle_update(self, update: dict) -> None:
        if update.get("@type") != NEW_MESSAGE_UPDATE:
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return

        source_id = message.get("chat_id")
        if type(source_id) is not int:
            return

        content = message.get("content")
        if isinstance(content, dict):
            if content.get("@type") in EXCLUDE_TYPES:
                return

        message_id = message.get("id")
        if type(message_id) is not int or message_id <= 0:
            return

        album_id = message.get("media_album_id") or 0
        if type(album_id) is not int or album_id == 0:
            self._enqueue_to_destinations(source_id, (message_id,))
            return

        self._buffer_album_message(source_id, album_id, message_id)

    def _buffer_album_message(
        self,
        source_id: int,
        album_id: int,
        message_id: int,
    ) -> None:
        key = (source_id, album_id)
        with self._lock:
            buffer = self._album_buffers.setdefault(key, set())
            buffer.add(message_id)
            self._cancel_timer_locked(key)
            timer = self._timer_factory(
                self._album_wait_seconds,
                self._flush_album,
                args=(source_id, album_id),
            )
            self._album_timers[key] = timer
            timer.start()

    def _flush_album(self, source_id: int, album_id: int) -> None:
        key = (source_id, album_id)
        with self._lock:
            message_ids = sorted(self._album_buffers.pop(key, ()))
            self._cancel_timer_locked(key)
        if message_ids:
            self._enqueue_to_destinations(source_id, tuple(message_ids))

    def _cancel_timer_locked(self, key: tuple[int, int]) -> None:
        timer = self._album_timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    def _enqueue_to_destinations(
        self,
        source_id: int,
        message_ids: tuple[int, ...],
    ) -> None:
        destinations = self._registry.destinations_for(source_id)
        if not destinations:
            return

        dynamic_pairs = {
            (task.source_id, task.destination_id)
            for task in self._registry.dynamic_tasks
        }
        builtin_pair = None
        if self._builtin_route is not None:
            builtin_pair = (
                self._builtin_route.source_id,
                self._builtin_route.destination_id,
            )

        for destination_id in destinations:
            route = Route(source_id, destination_id)
            pair = (source_id, destination_id)
            dynamic = pair in dynamic_pairs and pair != builtin_pair
            self._copy_service.enqueue_realtime(
                route,
                message_ids=message_ids,
                dynamic=dynamic,
            )
