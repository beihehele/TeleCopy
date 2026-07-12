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

    def enqueue_realtime(self, route, message_id, dynamic):
        self.items.append((route, message_id, dynamic))


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
        (Route(-1001, -2002), 10, True),
        (Route(-1001, -2001), 10, False),
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

    assert copy_service.items == [(Route(-1001, -2001), 10, False)]


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
