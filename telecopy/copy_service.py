"""Serialized forwarding scheduler and historical copy jobs."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import count
import logging
from queue import Empty, PriorityQueue
import re
from threading import Event, RLock, Semaphore, Thread
import time
from typing import Protocol

from telecopy.config import Route
from telecopy.database import CopyJob, StateStore
from telecopy.tasks import RouteRegistry
from telecopy.tdlib_client import EXCLUDE_TYPES, TdlibResponseError


logger = logging.getLogger(__name__)

REALTIME_PRIORITY = 0
HISTORY_PRIORITY = 10
MAX_COPY_ATTEMPTS = 5
MAX_FLOOD_WAIT = 300
DEFAULT_HISTORY_QUEUE_LIMIT = 100
_QUEUE_WAIT_SECONDS = 0.05

_FLOOD_WAIT_PATTERN = re.compile(
    r"(?:FLOOD_WAIT[_\s]+|retry\s+after\s+)(\d+)",
    re.IGNORECASE,
)


class ForwardingClient(Protocol):
    def iter_history(self, chat_id: int) -> Iterator[dict]: ...

    def forward_messages(
        self,
        source_id: int,
        destination_id: int,
        message_ids: Sequence[int],
        send_copy: bool,
    ) -> list[int | None]: ...


class ServiceStoppedError(RuntimeError):
    """Raised when work is submitted after shutdown starts."""


class HistoryJobActiveError(RuntimeError):
    """Raised when another history job is already active."""


class _ForwardInterruptedError(RuntimeError):
    """Raised when shutdown interrupts a retry delay."""


@dataclass(order=True)
class QueueItem:
    priority: int
    sequence: int
    route: Route = field(compare=False)
    message_ids: tuple[int, ...] = field(compare=False)
    dynamic_route: bool = field(compare=False)
    job_id: int | None = field(compare=False)


@dataclass(frozen=True)
class HistoryJobResult:
    job_id: int
    route: Route
    request_chat_id: int
    status: str
    copied_count: int
    error_message: str | None


@dataclass
class _HistoryJobState:
    job: CopyJob
    route: Route
    callback: Callable[[HistoryJobResult], None] | None
    pending_count: int = 0
    copied_count: int = 0
    producer_done: bool = False
    error_message: str | None = None
    finalized: bool = False


@dataclass(frozen=True)
class _Notification:
    result: HistoryJobResult
    callback: Callable[[HistoryJobResult], None] | None


WaitStrategy = Callable[[float, Event], bool]
ThreadFactory = Callable[..., Thread]


class CopyService:
    """Run forwarding and copy-record writes on one priority worker."""

    def __init__(
        self,
        client: ForwardingClient,
        store: StateStore,
        registry: RouteRegistry,
        send_copy: bool = True,
        *,
        wait_strategy: WaitStrategy | None = None,
        thread_factory: ThreadFactory = Thread,
        history_queue_limit: int = DEFAULT_HISTORY_QUEUE_LIMIT,
    ) -> None:
        if history_queue_limit <= 0:
            raise ValueError("history_queue_limit must be positive")
        self._client = client
        self._store = store
        self._registry = registry
        self._send_copy = send_copy
        self._wait_strategy = wait_strategy or self._wait_for_stop
        self._thread_factory = thread_factory
        self._queue: PriorityQueue[QueueItem] = PriorityQueue()
        self._history_slots = Semaphore(history_queue_limit)
        self._sequence = count()
        self._lock = RLock()
        self._stop_event = Event()
        self._accepting = True
        self._worker_thread: Thread | None = None
        self._producer_thread: Thread | None = None
        self._active_job: _HistoryJobState | None = None
        self._worker_fault: str | None = None

    @property
    def worker_fault(self) -> str | None:
        """Return the latest unexpected worker or persistence fault."""
        with self._lock:
            return self._worker_fault

    def start(self) -> None:
        """Start the single forwarding worker once."""
        with self._lock:
            self._ensure_accepting()
            if self._worker_thread is not None:
                return
            worker = self._thread_factory(
                target=self._worker_loop,
                name="telecopy-forwarding",
                daemon=True,
            )
            self._worker_thread = worker
            worker.start()

    def stop(self, timeout_seconds: float = 10.0) -> bool:
        """Stop within one deadline; return false if any thread remains active."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must not be negative")
        deadline = time.monotonic() + timeout_seconds
        with self._lock:
            self._accepting = False
            self._stop_event.set()
            producer = self._producer_thread
            worker = self._worker_thread

        self._join_until(producer, deadline)
        self._join_until(worker, deadline)
        if self._is_alive(producer) or self._is_alive(worker):
            return False

        self._discard_queued_items()
        if time.monotonic() > deadline:
            return False

        notification = None
        with self._lock:
            if self._active_job is not None:
                notification = self._finalize_job_locked(
                    self._active_job,
                    "interrupted",
                    "Copy service stopped",
                )
                if notification is None:
                    return False
        self._notify(notification)
        return True

    def enqueue_realtime(
        self,
        route: Route,
        message_id: int | None = None,
        dynamic: bool = False,
        *,
        message_ids: Sequence[int] | None = None,
    ) -> None:
        """Queue real-time work without waiting for history capacity."""
        ids = self._normalize_message_ids(message_id, message_ids)
        with self._lock:
            self._ensure_accepting()
            self._queue.put(
                QueueItem(
                    REALTIME_PRIORITY,
                    next(self._sequence),
                    route,
                    ids,
                    dynamic,
                    None,
                )
            )

    def enqueue_history(
        self,
        route: Route,
        message_id: int | None = None,
        job_id: int | None = None,
        *,
        message_ids: Sequence[int] | None = None,
    ) -> None:
        """Queue historical work with interruptible bounded backpressure."""
        if job_id is None:
            raise TypeError("job_id is required")
        ids = self._normalize_message_ids(message_id, message_ids)
        if not self._acquire_history_slot():
            raise ServiceStoppedError("Copy service is stopping")
        with self._lock:
            try:
                self._ensure_accepting()
                self._queue.put(
                    QueueItem(
                        HISTORY_PRIORITY,
                        next(self._sequence),
                        route,
                        ids,
                        False,
                        job_id,
                    )
                )
            except BaseException:
                self._history_slots.release()
                raise

    def start_history_job(
        self,
        route: Route,
        requested_by: int,
        request_chat_id: int,
        callback: Callable[[HistoryJobResult], None] | None = None,
    ) -> CopyJob:
        """Persist and start the sole active history producer."""
        with self._lock:
            self._ensure_accepting()
            if self._active_job is not None:
                raise HistoryJobActiveError(
                    "Another history copy job is already active"
                )
            job = self._store.create_copy_job(
                route.source_id,
                route.destination_id,
                requested_by,
                request_chat_id,
            )
            state = _HistoryJobState(job, route, callback)
            self._active_job = state
            producer = self._thread_factory(
                target=lambda: self._produce_history(state),
                name=f"telecopy-history-{job.id}",
                daemon=True,
            )
            self._producer_thread = producer
            try:
                producer.start()
            except BaseException as error:
                notification = self._finalize_job_locked(
                    state,
                    "failed",
                    str(error),
                )
                self._notify(notification)
                raise
            return job

    def _process_next_for_test(self) -> bool:
        """Process one item only when the production worker is not running."""
        worker = self._worker_thread
        if self._is_alive(worker):
            raise RuntimeError("Cannot process manually while worker is running")
        processed = self._process_one_queued_item(block=False)
        if not processed:
            self._retry_ready_finalization()
        return processed

    def _produce_history(self, state: _HistoryJobState) -> None:
        error_message = None
        try:
            with self._lock:
                if self._active_job is not state or state.finalized:
                    return
                self._store.update_copy_job(
                    state.job.id,
                    "running",
                    state.copied_count,
                )

            pending_album_id: int | None = None
            pending_ids: list[int] = []

            def flush_pending() -> bool:
                nonlocal pending_album_id, pending_ids
                if not pending_ids:
                    return True
                ids = tuple(sorted(pending_ids))
                pending_ids = []
                pending_album_id = None
                return self._enqueue_produced_history(state, ids)

            for message in self._client.iter_history(state.route.source_id):
                if self._stop_event.is_set():
                    break
                if not isinstance(message, dict):
                    raise ValueError("History contains a malformed message")
                content = message.get("content")
                if isinstance(content, dict) and content.get("@type") in EXCLUDE_TYPES:
                    continue
                message_id = message.get("id")
                self._validate_message_id(
                    message_id,
                    "History contains an invalid message ID",
                )
                album_id = message.get("media_album_id") or 0
                if type(album_id) is not int:
                    album_id = 0
                if album_id == 0:
                    if not flush_pending():
                        break
                    if not self._enqueue_produced_history(
                        state,
                        (message_id,),
                    ):
                        break
                    continue
                if pending_album_id is not None and album_id != pending_album_id:
                    if not flush_pending():
                        break
                pending_album_id = album_id
                pending_ids.append(message_id)
            flush_pending()
        except Exception as error:
            error_message = str(error)
            self._record_fault(error)

        with self._lock:
            if self._active_job is not state or state.finalized:
                return
            state.producer_done = True
            if error_message is not None and state.error_message is None:
                state.error_message = error_message
        self._retry_ready_finalization()

    def _enqueue_produced_history(
        self,
        state: _HistoryJobState,
        message_ids: Sequence[int],
    ) -> bool:
        ids = tuple(message_ids)
        if not ids:
            return True
        for message_id in ids:
            self._validate_message_id(
                message_id,
                "History contains an invalid message ID",
            )
        if not self._acquire_history_slot():
            return False
        with self._lock:
            if (
                self._stop_event.is_set()
                or self._active_job is not state
                or state.finalized
            ):
                self._history_slots.release()
                return False
            state.pending_count += 1
            self._queue.put(
                QueueItem(
                    HISTORY_PRIORITY,
                    next(self._sequence),
                    state.route,
                    ids,
                    False,
                    state.job.id,
                )
            )
            return True

    def _acquire_history_slot(self) -> bool:
        while not self._stop_event.is_set():
            if self._history_slots.acquire(timeout=_QUEUE_WAIT_SECONDS):
                return True
        return False

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._process_one_queued_item(block=True):
                self._retry_ready_finalization()

    def _process_one_queued_item(self, *, block: bool) -> bool:
        try:
            if block:
                item = self._queue.get(timeout=_QUEUE_WAIT_SECONDS)
            else:
                item = self._queue.get_nowait()
        except Empty:
            return False

        try:
            self._process_item(item)
        except Exception as error:
            self._record_fault(error)
            logger.exception("Forwarding worker item failed unexpectedly")
        finally:
            self._queue.task_done()
            if item.job_id is not None:
                self._history_slots.release()
        return True

    def _process_item(self, item: QueueItem) -> None:
        copied_count = None
        error_message = None
        try:
            if item.dynamic_route and not self._registry.contains(item.route):
                return
            pending_ids = tuple(
                message_id
                for message_id in item.message_ids
                if not self._store.was_copied(
                    item.route.source_id,
                    item.route.destination_id,
                    message_id,
                )
            )
            if not pending_ids:
                return

            destination_message_ids = self._forward_with_retry(
                item.route,
                pending_ids,
            )
            for source_message_id, destination_message_id in zip(
                pending_ids,
                destination_message_ids,
                strict=True,
            ):
                if destination_message_id is None:
                    continue
                self._store.record_copy(
                    item.route.source_id,
                    item.route.destination_id,
                    source_message_id,
                    destination_message_id,
                )
                if item.job_id is not None:
                    copied_count = self._store.increment_copy_job_progress(
                        item.job_id
                    )
        except _ForwardInterruptedError:
            pass
        except Exception as error:
            error_message = str(error)
            logger.error(
                "Could not forward messages %s on route %d -> %d: %s",
                item.message_ids,
                item.route.source_id,
                item.route.destination_id,
                error,
            )
        finally:
            if item.job_id is not None:
                self._complete_history_item(
                    item.job_id,
                    copied_count,
                    error_message,
                )

    def _forward_with_retry(
        self,
        route: Route,
        message_ids: tuple[int, ...],
    ) -> list[int | None]:
        for attempt in range(1, MAX_COPY_ATTEMPTS + 1):
            try:
                return self._client.forward_messages(
                    route.source_id,
                    route.destination_id,
                    message_ids,
                    self._send_copy,
                )
            except Exception as error:
                wait_seconds = self._retry_delay(error, attempt)
                if wait_seconds is None or attempt >= MAX_COPY_ATTEMPTS:
                    raise
                if self._wait_strategy(wait_seconds, self._stop_event):
                    raise _ForwardInterruptedError from None
        raise RuntimeError("Forward retry loop ended unexpectedly")

    @staticmethod
    def _retry_delay(error: Exception, attempt: int) -> int | None:
        if not isinstance(error, TdlibResponseError):
            return None
        message = error.tdlib_message or ""
        flood_wait = _FLOOD_WAIT_PATTERN.search(message)
        if flood_wait is not None and (
            error.code == 429 or "FLOOD_WAIT" in message.upper()
        ):
            return min(int(flood_wait.group(1)), MAX_FLOOD_WAIT)
        if error.code is not None and 500 <= error.code <= 599:
            return 2 ** attempt
        return None

    def _complete_history_item(
        self,
        job_id: int,
        copied_count: int | None,
        error_message: str | None,
    ) -> None:
        with self._lock:
            state = self._active_job
            if state is None or state.job.id != job_id or state.finalized:
                return
            state.pending_count -= 1
            if copied_count is not None:
                state.copied_count = copied_count
            if error_message is not None and state.error_message is None:
                state.error_message = error_message
        self._retry_ready_finalization()

    def _retry_ready_finalization(self) -> None:
        notification = None
        with self._lock:
            state = self._active_job
            if state is None:
                return
            notification = self._finish_job_if_ready_locked(state)
        self._notify(notification)

    def _finish_job_if_ready_locked(
        self,
        state: _HistoryJobState,
    ) -> _Notification | None:
        if self._stop_event.is_set():
            return None
        if not state.producer_done or state.pending_count != 0:
            return None
        if state.error_message is not None:
            return self._finalize_job_locked(
                state,
                "failed",
                state.error_message,
            )
        return self._finalize_job_locked(state, "completed", None)

    def _finalize_job_locked(
        self,
        state: _HistoryJobState,
        status: str,
        error_message: str | None,
    ) -> _Notification | None:
        if state.finalized:
            return None
        try:
            self._store.update_copy_job(
                state.job.id,
                status,
                state.copied_count,
                error_message,
            )
        except Exception as error:
            self._record_fault(error)
            logger.exception(
                "Could not finalize history job %d as %s",
                state.job.id,
                status,
            )
            return None

        state.finalized = True
        if self._active_job is state:
            self._active_job = None
            self._producer_thread = None
        return _Notification(
            HistoryJobResult(
                state.job.id,
                state.route,
                state.job.request_chat_id,
                status,
                state.copied_count,
                error_message,
            ),
            state.callback,
        )

    def _discard_queued_items(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                return
            self._queue.task_done()
            if item.job_id is not None:
                self._history_slots.release()

    def _record_fault(self, error: Exception) -> None:
        with self._lock:
            self._worker_fault = f"{type(error).__name__}: {error}"

    @staticmethod
    def _join_until(thread: Thread | None, deadline: float) -> None:
        if thread is None or not thread.is_alive():
            return
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)

    @staticmethod
    def _is_alive(thread: Thread | None) -> bool:
        return thread is not None and thread.is_alive()

    @staticmethod
    def _notify(notification: _Notification | None) -> None:
        if notification is None or notification.callback is None:
            return
        try:
            notification.callback(notification.result)
        except Exception:
            logger.exception(
                "History job %d notification failed",
                notification.result.job_id,
            )

    def _ensure_accepting(self) -> None:
        if not self._accepting:
            raise ServiceStoppedError("Copy service is stopping")

    @classmethod
    def _normalize_message_ids(
        cls,
        message_id: int | None,
        message_ids: Sequence[int] | None,
    ) -> tuple[int, ...]:
        if message_ids is not None and message_id is not None:
            raise ValueError("Pass message_id or message_ids, not both")
        if message_ids is not None:
            ids = tuple(message_ids)
        elif message_id is not None:
            ids = (message_id,)
        else:
            raise ValueError("message_id or message_ids is required")
        if not ids:
            raise ValueError("message_ids must not be empty")
        for value in ids:
            cls._validate_message_id(value)
        return ids

    @staticmethod
    def _validate_message_id(
        message_id: int,
        message: str = "message_id must be a positive integer",
    ) -> None:
        if type(message_id) is not int or message_id <= 0:
            raise ValueError(message)

    @staticmethod
    def _wait_for_stop(seconds: float, stop_event: Event) -> bool:
        return stop_event.wait(seconds)
