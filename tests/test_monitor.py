from telecopy.config import Route
from telecopy.copy_service import CopyService
from telecopy.database import StateStore, WatchTask
from telecopy.monitor import EXCLUDE_TYPES, MonitorDispatcher
from telecopy.tasks import RouteRegistry


class FakeTdlibClient:
    def __init__(self):
        self.handlers = []

    def add_new_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_new_message_handler(self, handler):
        self.handlers.remove(handler)


class RecordingCopyService:
    def __init__(self):
        self.items = []

    def enqueue_realtime(self, route, message_ids, dynamic):
        if isinstance(message_ids, int):
            message_ids = (message_ids,)
        self.items.append((route, tuple(message_ids), dynamic))


class FakeTimer:
    instances = []

    def __init__(self, interval, function, args=None, kwargs=None):
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        return None

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.function(*self.args, **self.kwargs)


def make_registry(builtin=None, dynamic=()):
    registry = RouteRegistry(builtin)
    registry.replace_dynamic(dynamic)
    return registry


def test_dispatcher_ignores_non_message_updates():
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(),
        None,
    )

    dispatcher.handle_update({"@type": "updateAuthorizationState"})

    assert copy_service.items == []


def test_dispatcher_ignores_excluded_service_messages():
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    builtin = Route(-1001, -2001)
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(builtin),
        builtin,
    )

    dispatcher.handle_update(
        {
            "@type": "updateNewMessage",
            "message": {
                "id": 10,
                "chat_id": -1001,
                "content": {"@type": "messagePinMessage"},
            },
        }
    )

    assert copy_service.items == []
    assert "messagePinMessage" in EXCLUDE_TYPES


def test_dispatcher_ignores_unmatched_sources():
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    builtin = Route(-1001, -2001)
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(builtin),
        builtin,
    )

    dispatcher.handle_update(
        {
            "@type": "updateNewMessage",
            "message": {
                "id": 10,
                "chat_id": -9999,
                "content": {"@type": "messageText"},
            },
        }
    )

    assert copy_service.items == []


def test_dispatcher_enqueues_one_source_to_multiple_destinations():
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    builtin = Route(-1001, -2001)
    dynamic = (
        WatchTask(-1001, -2002, 42, "2026-07-12T00:00:00+00:00"),
    )
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(builtin, dynamic),
        builtin,
    )

    dispatcher.handle_update(
        {
            "@type": "updateNewMessage",
            "message": {
                "id": 10,
                "chat_id": -1001,
                "content": {"@type": "messageText"},
            },
        }
    )

    assert copy_service.items == [
        (Route(-1001, -2002), (10,), True),
        (Route(-1001, -2001), (10,), False),
    ]


def test_dispatcher_deduplicates_identical_builtin_and_dynamic_routes():
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    builtin = Route(-1001, -2001)
    dynamic = (
        WatchTask(-1001, -2001, 42, "2026-07-12T00:00:00+00:00"),
    )
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(builtin, dynamic),
        builtin,
    )

    dispatcher.handle_update(
        {
            "@type": "updateNewMessage",
            "message": {
                "id": 10,
                "chat_id": -1001,
                "content": {"@type": "messageText"},
            },
        }
    )

    assert copy_service.items == [(Route(-1001, -2001), (10,), False)]


def test_dispatcher_buffers_album_and_forwards_as_one_batch():
    FakeTimer.instances.clear()
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    builtin = Route(-1001, -2001)
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(builtin),
        builtin,
        album_wait_seconds=1.0,
        timer_factory=FakeTimer,
    )

    for message_id in (11, 12, 13):
        dispatcher.handle_update(
            {
                "@type": "updateNewMessage",
                "message": {
                    "id": message_id,
                    "chat_id": -1001,
                    "media_album_id": 555,
                    "content": {"@type": "messageVideo"},
                },
            }
        )

    assert copy_service.items == []
    assert len(FakeTimer.instances) == 3
    FakeTimer.instances[-1].fire()

    assert copy_service.items == [
        (Route(-1001, -2001), (11, 12, 13), False),
    ]


def test_dispatcher_buffers_album_when_album_id_is_string():
    FakeTimer.instances.clear()
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    builtin = Route(-1001, -2001)
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(builtin),
        builtin,
        album_wait_seconds=1.0,
        timer_factory=FakeTimer,
    )

    for message_id in (11, 12, 13):
        dispatcher.handle_update(
            {
                "@type": "updateNewMessage",
                "message": {
                    "id": message_id,
                    "chat_id": -1001,
                    "media_album_id": "9223372036854775807",
                    "content": {"@type": "messageVideo"},
                },
            }
        )

    assert copy_service.items == []
    FakeTimer.instances[-1].fire()

    assert copy_service.items == [
        (Route(-1001, -2001), (11, 12, 13), False),
    ]


def test_dispatcher_forwards_non_album_immediately():
    FakeTimer.instances.clear()
    client = FakeTdlibClient()
    copy_service = RecordingCopyService()
    builtin = Route(-1001, -2001)
    dispatcher = MonitorDispatcher(
        client,
        copy_service,
        make_registry(builtin),
        builtin,
        album_wait_seconds=1.0,
        timer_factory=FakeTimer,
    )

    dispatcher.handle_update(
        {
            "@type": "updateNewMessage",
            "message": {
                "id": 10,
                "chat_id": -1001,
                "media_album_id": 0,
                "content": {"@type": "messageText"},
            },
        }
    )

    assert copy_service.items == [(Route(-1001, -2001), (10,), False)]
    assert FakeTimer.instances == []


def test_dispatcher_registers_and_removes_handler():
    client = FakeTdlibClient()
    dispatcher = MonitorDispatcher(
        client,
        RecordingCopyService(),
        make_registry(),
        None,
    )

    dispatcher.start()
    assert len(client.handlers) == 1

    dispatcher.stop()
    assert client.handlers == []
