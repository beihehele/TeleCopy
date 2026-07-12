import sqlite3
from threading import Event
import time

import pytest

from telecopy.config import Route
from telecopy.copy_service import (
    HISTORY_PRIORITY,
    MAX_COPY_ATTEMPTS,
    MAX_FLOOD_WAIT,
    REALTIME_PRIORITY,
    CopyService,
    HistoryJobActiveError,
    ServiceStoppedError,
)
from telecopy.database import ActiveCopyJobError, StateStore, WatchTask
from telecopy.tasks import RouteRegistry
from telecopy.tdlib_client import TdlibResponseError


class FakeClient:
    def __init__(self, history=()):
        self.forwarded = []
        self.history = list(history)
        self.responses = []

    def iter_history(self, source_id):
        yield from self.history

    def forward_messages(
        self,
        source_id,
        destination_id,
        message_ids,
        send_copy,
    ):
        ids = tuple(message_ids)
        self.forwarded.append(
            (source_id, destination_id, ids, send_copy)
        )
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, list):
                return response
            return [response]
        return [message_id + 1000 for message_id in ids]

    def forward_message(
        self,
        source_id,
        destination_id,
        message_id,
        send_copy,
    ):
        return self.forward_messages(
            source_id,
            destination_id,
            [message_id],
            send_copy,
        )[0]


class CapturingThreadFactory:
    def __init__(self):
        self.targets = []

    def __call__(self, *, target, name, daemon):
        factory = self

        class CapturedThread:
            def start(self):
                factory.targets.append(target)

            def join(self, timeout=None):
                return None

            def is_alive(self):
                return False

        return CapturedThread()


@pytest.fixture
def store(tmp_path):
    state_store = StateStore(tmp_path / "telecopy.db")
    state_store.initialize()
    return state_store


@pytest.fixture
def route():
    return Route(-1001, -2001)


@pytest.fixture
def registry(route):
    return RouteRegistry(route)


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def service(store, registry, fake_client):
    return CopyService(fake_client, store, registry, send_copy=True)


def read_job(store, job_id):
    with sqlite3.connect(store.database_path) as connection:
        return connection.execute(
            """
            SELECT status, copied_count, error_message
            FROM copy_jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()


def test_queue_item_priorities_are_public_contract():
    assert REALTIME_PRIORITY == 0
    assert HISTORY_PRIORITY == 10


def test_realtime_item_runs_before_queued_history_item(
    service,
    fake_client,
    route,
):
    service.enqueue_history(route, message_id=1, job_id=7)
    service.enqueue_realtime(route, message_id=2, dynamic=False)

    service._process_next_for_test()

    assert fake_client.forwarded[0][2] == (2,)


def test_same_priority_uses_stable_enqueue_sequence(
    service,
    fake_client,
    route,
):
    service.enqueue_realtime(route, message_id=2, dynamic=False)
    service.enqueue_realtime(route, message_id=1, dynamic=False)

    service._process_next_for_test()
    service._process_next_for_test()

    assert [call[2] for call in fake_client.forwarded] == [(2,), (1,)]


def test_copy_record_deduplicates_forwarding(service, fake_client, store, route):
    store.record_copy(route.source_id, route.destination_id, 2, 99)
    service.enqueue_realtime(route, message_id=2, dynamic=False)

    service._process_next_for_test()

    assert fake_client.forwarded == []


def test_removed_dynamic_route_is_skipped(store, fake_client, route):
    registry = RouteRegistry(None)
    registry.replace_dynamic(
        [WatchTask(route.source_id, route.destination_id, 1, "now")]
    )
    service = CopyService(fake_client, store, registry)
    service.enqueue_realtime(route, message_id=2, dynamic=True)
    registry.replace_dynamic([])

    service._process_next_for_test()

    assert fake_client.forwarded == []


def test_removed_dynamic_copy_still_runs_when_builtin_route_exists(
    store,
    fake_client,
    route,
):
    registry = RouteRegistry(route)
    registry.replace_dynamic(
        [WatchTask(route.source_id, route.destination_id, 1, "now")]
    )
    service = CopyService(fake_client, store, registry)
    service.enqueue_realtime(route, message_id=2, dynamic=True)
    registry.replace_dynamic([])

    service._process_next_for_test()

    assert fake_client.forwarded[0][2] == (2,)


def test_success_is_recorded_only_after_forwarding(
    service,
    fake_client,
    store,
    route,
):
    service.enqueue_realtime(route, message_id=2, dynamic=False)

    service._process_next_for_test()

    assert store.was_copied(
        route.source_id,
        route.destination_id,
        2,
    )
    assert fake_client.forwarded == [
        (route.source_id, route.destination_id, (2,), True)
    ]


def test_album_batch_is_forwarded_and_recorded_together(
    service,
    fake_client,
    store,
    route,
):
    service.enqueue_realtime(route, message_ids=(11, 12, 13), dynamic=False)

    service._process_next_for_test()

    assert fake_client.forwarded == [
        (route.source_id, route.destination_id, (11, 12, 13), True)
    ]
    assert store.was_copied(route.source_id, route.destination_id, 11)
    assert store.was_copied(route.source_id, route.destination_id, 12)
    assert store.was_copied(route.source_id, route.destination_id, 13)


def test_history_job_groups_album_messages_into_one_forward(
    store,
    registry,
    fake_client,
    route,
):
    fake_client.history = [
        {
            "id": 13,
            "media_album_id": 77,
            "content": {"@type": "messageVideo"},
        },
        {
            "id": 12,
            "media_album_id": 77,
            "content": {"@type": "messageVideo"},
        },
        {
            "id": 11,
            "media_album_id": 77,
            "content": {"@type": "messageVideo"},
        },
        {
            "id": 5,
            "media_album_id": 0,
            "content": {"@type": "messageText"},
        },
    ]
    factory = CapturingThreadFactory()
    service = CopyService(
        fake_client,
        store,
        registry,
        thread_factory=factory,
    )
    results = []

    job = service.start_history_job(route, 42, 99, callback=results.append)
    factory.targets[0]()
    while service._process_next_for_test():
        pass

    assert fake_client.forwarded == [
        (route.source_id, route.destination_id, (11, 12, 13), True),
        (route.source_id, route.destination_id, (5,), True),
    ]
    assert results[0].status == "completed"
    assert results[0].copied_count == 4
    assert job.id == results[0].job_id


def test_flood_wait_uses_server_delay_with_cap_and_bounded_attempts(
    store,
    registry,
    fake_client,
    route,
):
    waits = []
    fake_client.responses = [
        TdlibResponseError(
            "flood",
            code=429,
            tdlib_message="FLOOD_WAIT_999",
        )
        for _ in range(MAX_COPY_ATTEMPTS)
    ]
    service = CopyService(
        fake_client,
        store,
        registry,
        wait_strategy=lambda seconds, stop_event: waits.append(seconds)
        or False,
    )
    service.enqueue_realtime(route, message_id=2, dynamic=False)

    service._process_next_for_test()

    assert len(fake_client.forwarded) == MAX_COPY_ATTEMPTS
    assert waits == [MAX_FLOOD_WAIT] * (MAX_COPY_ATTEMPTS - 1)
    assert not store.was_copied(route.source_id, route.destination_id, 2)


def test_non_flood_errors_use_exponential_backoff(
    store,
    registry,
    fake_client,
    route,
):
    waits = []
    fake_client.responses = [
        TdlibResponseError("temporary", code=500),
        TdlibResponseError("temporary", code=500),
        77,
    ]
    service = CopyService(
        fake_client,
        store,
        registry,
        wait_strategy=lambda seconds, stop_event: waits.append(seconds)
        or False,
    )
    service.enqueue_realtime(route, message_id=2, dynamic=False)

    service._process_next_for_test()

    assert waits == [2, 4]
    assert store.was_copied(route.source_id, route.destination_id, 2)


def test_history_job_finishes_only_after_all_items_are_processed(
    store,
    registry,
    route,
):
    client = FakeClient([{"id": 3}, {"id": 2}])
    threads = CapturingThreadFactory()
    notifications = []
    service = CopyService(
        client,
        store,
        registry,
        thread_factory=threads,
    )

    job = service.start_history_job(
        route,
        requested_by=10,
        request_chat_id=20,
        callback=notifications.append,
    )
    threads.targets.pop()()

    assert read_job(store, job.id)[0] == "running"
    assert notifications == []
    service._process_next_for_test()
    assert read_job(store, job.id)[0] == "running"
    service._process_next_for_test()

    assert read_job(store, job.id) == ("completed", 2, None)
    assert notifications[0].status == "completed"
    assert notifications[0].request_chat_id == 20


def test_history_job_failure_waits_for_enqueued_items_and_notifies(
    store,
    registry,
    route,
):
    client = FakeClient([{"id": 3}, {"id": 2}])
    client.responses = [TdlibResponseError("permanent", code=400)]
    threads = CapturingThreadFactory()
    notifications = []
    service = CopyService(
        client,
        store,
        registry,
        wait_strategy=lambda seconds, stop_event: False,
        thread_factory=threads,
    )
    job = service.start_history_job(
        route,
        requested_by=10,
        request_chat_id=20,
        callback=notifications.append,
    )
    threads.targets.pop()()

    service._process_next_for_test()
    assert read_job(store, job.id)[0] == "running"
    service._process_next_for_test()

    status, copied_count, error_message = read_job(store, job.id)
    assert status == "failed"
    assert copied_count == 1
    assert "permanent" in error_message
    assert notifications[0].status == "failed"


def test_history_copy_progress_is_skipped_when_job_is_resubmitted(
    store,
    registry,
    route,
):
    store.record_copy(route.source_id, route.destination_id, 2, 88)
    client = FakeClient([{"id": 2}])
    threads = CapturingThreadFactory()
    notifications = []
    service = CopyService(
        client,
        store,
        registry,
        thread_factory=threads,
    )
    job = service.start_history_job(
        route,
        requested_by=10,
        request_chat_id=20,
        callback=notifications.append,
    )

    threads.targets.pop()()
    service._process_next_for_test()

    assert read_job(store, job.id) == ("completed", 0, None)
    assert client.forwarded == []
    assert notifications[0].status == "completed"


def test_only_one_history_job_can_be_active_globally(
    store,
    registry,
    route,
):
    threads = CapturingThreadFactory()
    service = CopyService(
        FakeClient(),
        store,
        registry,
        thread_factory=threads,
    )
    service.start_history_job(route, 10, 20)

    with pytest.raises(HistoryJobActiveError):
        service.start_history_job(Route(-3001, -4001), 10, 20)


def test_duplicate_active_route_uses_database_exception(
    store,
    registry,
    route,
):
    store.create_copy_job(route.source_id, route.destination_id, 1, 2)
    service = CopyService(FakeClient(), store, registry)

    with pytest.raises(ActiveCopyJobError):
        service.start_history_job(route, 10, 20)


def test_stop_rejects_new_work_and_interrupts_active_job(
    store,
    registry,
    route,
):
    threads = CapturingThreadFactory()
    service = CopyService(
        FakeClient([{"id": 2}]),
        store,
        registry,
        thread_factory=threads,
    )
    job = service.start_history_job(route, 10, 20)

    stopped = service.stop(timeout_seconds=0.5)

    assert stopped is True
    assert read_job(store, job.id)[0] == "interrupted"
    with pytest.raises(ServiceStoppedError):
        service.enqueue_realtime(route, 2, dynamic=False)
    with pytest.raises(ServiceStoppedError):
        service.start_history_job(Route(-3001, -4001), 10, 20)


def test_stop_event_interrupts_retry_wait(
    store,
    registry,
    fake_client,
    route,
):
    fake_client.responses = [
        TdlibResponseError(
            "flood",
            code=429,
            tdlib_message="FLOOD_WAIT_30",
        )
    ]
    waits = []

    def interrupt_wait(seconds, stop_event):
        waits.append(seconds)
        stop_event.set()
        return True

    service = CopyService(
        fake_client,
        store,
        registry,
        wait_strategy=interrupt_wait,
    )
    service.enqueue_realtime(route, 2, dynamic=False)

    service._process_next_for_test()

    assert waits == [30]
    assert len(fake_client.forwarded) == 1


def test_permanent_400_is_not_retried(
    store,
    registry,
    fake_client,
    route,
):
    fake_client.responses = [
        TdlibResponseError("bad request", code=400),
        77,
    ]
    service = CopyService(fake_client, store, registry)
    service.enqueue_realtime(route, 2, dynamic=False)

    service._process_next_for_test()

    assert len(fake_client.forwarded) == 1


def test_unknown_forward_exception_is_not_retried(
    store,
    registry,
    fake_client,
    route,
):
    fake_client.responses = [RuntimeError("unknown"), 77]
    service = CopyService(fake_client, store, registry)
    service.enqueue_realtime(route, 2, dynamic=False)

    service._process_next_for_test()

    assert len(fake_client.forwarded) == 1


class BackpressureClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.second_message_requested = Event()
        self.history_finished = Event()

    def iter_history(self, source_id):
        yield {"id": 1}
        self.second_message_requested.set()
        yield {"id": 2}
        self.history_finished.set()


def test_history_backpressure_does_not_block_realtime_priority(
    store,
    registry,
    route,
):
    client = BackpressureClient()
    service = CopyService(
        client,
        store,
        registry,
        history_queue_limit=1,
    )
    service.start_history_job(route, 10, 20)
    assert client.second_message_requested.wait(timeout=1)

    service.enqueue_realtime(route, 3, dynamic=False)
    service._process_next_for_test()

    assert client.forwarded[0][2] == (3,)
    service._process_next_for_test()
    assert client.history_finished.wait(timeout=1)
    service._process_next_for_test()
    assert service.stop(timeout_seconds=1) is True


def test_stop_interrupts_history_backpressure_wait(
    store,
    registry,
    route,
):
    client = BackpressureClient()
    service = CopyService(
        client,
        store,
        registry,
        history_queue_limit=1,
    )
    job = service.start_history_job(route, 10, 20)
    assert client.second_message_requested.wait(timeout=1)

    started_at = time.monotonic()
    stopped = service.stop(timeout_seconds=0.5)

    assert stopped is True
    assert time.monotonic() - started_at < 0.5
    assert read_job(store, job.id)[0] == "interrupted"


class BlockingForwardClient(FakeClient):
    def __init__(self):
        super().__init__([{"id": 1}])
        self.forward_started = Event()
        self.release_forward = Event()
        self.forward_finished = Event()

    def forward_messages(
        self,
        source_id,
        destination_id,
        message_ids,
        send_copy,
    ):
        ids = tuple(message_ids)
        self.forwarded.append(
            (source_id, destination_id, ids, send_copy)
        )
        self.forward_started.set()
        try:
            assert self.release_forward.wait(timeout=2)
            return [message_id + 1000 for message_id in ids]
        finally:
            self.forward_finished.set()


def test_blocking_forward_makes_stop_timeout_without_finalizing_job(
    store,
    registry,
    route,
):
    client = BlockingForwardClient()
    service = CopyService(client, store, registry)
    service.start()
    job = service.start_history_job(route, 10, 20)
    assert client.forward_started.wait(timeout=1)

    started_at = time.monotonic()
    stopped = service.stop(timeout_seconds=0.05)

    assert stopped is False
    assert time.monotonic() - started_at < 0.5
    assert read_job(store, job.id)[0] == "running"

    client.release_forward.set()
    assert client.forward_finished.wait(timeout=1)
    assert service.stop(timeout_seconds=1) is True
    assert read_job(store, job.id)[0:2] == ("interrupted", 1)


def test_finalize_database_failure_is_retried_without_losing_active_job(
    store,
    registry,
    route,
    monkeypatch,
):
    threads = CapturingThreadFactory()
    service = CopyService(
        FakeClient([{"id": 1}]),
        store,
        registry,
        thread_factory=threads,
    )
    original_update = store.update_copy_job
    failed_once = False

    def fail_first_completion(
        job_id,
        status,
        copied_count,
        error_message=None,
    ):
        nonlocal failed_once
        if status == "completed" and not failed_once:
            failed_once = True
            raise sqlite3.OperationalError("finalize unavailable")
        return original_update(job_id, status, copied_count, error_message)

    monkeypatch.setattr(store, "update_copy_job", fail_first_completion)
    job = service.start_history_job(route, 10, 20)
    threads.targets.pop()()

    service._process_next_for_test()

    assert read_job(store, job.id)[0:2] == ("running", 1)
    assert service.worker_fault is not None
    assert service._process_next_for_test() is False
    assert read_job(store, job.id)[0:2] == ("completed", 1)


class SignalingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.forwarded_event = Event()

    def forward_messages(
        self,
        source_id,
        destination_id,
        message_ids,
        send_copy,
    ):
        result = super().forward_messages(
            source_id,
            destination_id,
            message_ids,
            send_copy,
        )
        self.forwarded_event.set()
        return result


def test_worker_reports_item_exception_and_continues(
    store,
    registry,
    route,
    monkeypatch,
):
    client = SignalingClient()
    service = CopyService(client, store, registry)
    original_process = service._process_item
    raised_once = False

    def raise_once(item):
        nonlocal raised_once
        if not raised_once:
            raised_once = True
            raise RuntimeError("worker item failure")
        original_process(item)

    monkeypatch.setattr(service, "_process_item", raise_once)
    service.enqueue_realtime(route, 1, dynamic=False)
    service.enqueue_realtime(route, 2, dynamic=False)
    service.start()

    assert client.forwarded_event.wait(timeout=1)
    assert service.worker_fault is not None
    assert "worker item failure" in service.worker_fault
    assert service.stop(timeout_seconds=1) is True


def test_private_process_hook_rejects_running_worker(
    store,
    registry,
):
    service = CopyService(FakeClient(), store, registry)
    service.start()

    with pytest.raises(RuntimeError, match="worker"):
        service._process_next_for_test()

    assert service.stop(timeout_seconds=1) is True
