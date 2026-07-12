import json
from io import BytesIO
from pathlib import Path
import http.client
import logging
import urllib.error
import urllib.request

import pytest

from telecopy.admin_bot import AdminBot, AdminCommands, CommandError
from telecopy.config import AppConfig, Route
from telecopy.copy_service import CopyService
from telecopy.database import StateStore
from telecopy.tasks import RouteRegistry


ENVIRONMENT_KEYS = (
    "PHONE",
    "API_ID",
    "API_HASH",
    "DB_PASSWORD",
    "SOURCE",
    "DESTINATION",
    "BOT_TOKEN",
    "BOT_ADMIN_IDS",
    "FILES_DIRECTORY",
    "SEND_COPY",
    "PROXY_URL",
)


@pytest.fixture(autouse=True)
def clear_config_environment(monkeypatch):
    monkeypatch.setattr("telecopy.config.load_dotenv", lambda: None)
    for key in ENVIRONMENT_KEYS:
        monkeypatch.delenv(key, raising=False)


def set_required_env(monkeypatch):
    monkeypatch.setenv("PHONE", "+15551234567")
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", "api-hash")
    monkeypatch.setenv("DB_PASSWORD", "database-password")


def make_config(monkeypatch, **overrides):
    set_required_env(monkeypatch)
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    return AppConfig.from_env()


class FakeTdlib:
    def __init__(self, accessible=()):
        self.accessible = set(accessible)

    def validate_chat(self, chat_id):
        return chat_id in self.accessible


class FakeCopyService:
    def __init__(self):
        self.jobs = []
        self.raise_active = False

    def start_history_job(self, route, requested_by, request_chat_id, callback):
        if self.raise_active:
            from telecopy.copy_service import HistoryJobActiveError

            raise HistoryJobActiveError("busy")
        job = type(
            "Job",
            (),
            {"id": len(self.jobs) + 1},
        )()
        self.jobs.append((route, requested_by, request_chat_id, callback))
        return job


@pytest.fixture
def store(tmp_path):
    state_store = StateStore(tmp_path / "telecopy.db")
    state_store.initialize()
    return state_store


@pytest.fixture
def registry():
    return RouteRegistry(Route(-1001, -2001))


@pytest.fixture
def commands(monkeypatch, store, registry):
    config = make_config(
        monkeypatch,
        DESTINATION="-2001",
        BOT_TOKEN="bot-token",
        BOT_ADMIN_IDS="42",
    )
    return AdminCommands(
        config,
        store,
        registry,
        FakeTdlib({-1001, -1002, -2002, -2003}),
        FakeCopyService(),
    )


def test_watch_requires_private_admin(commands):
    with pytest.raises(CommandError, match="Unauthorized"):
        commands.watch(99, ["-1002"])


def test_watch_uses_default_destination(monkeypatch, store, registry):
    config = make_config(
        monkeypatch,
        DESTINATION="-2002",
        BOT_ADMIN_IDS="42",
    )
    service = FakeCopyService()
    admin = AdminCommands(
        config,
        store,
        registry,
        FakeTdlib({-1003, -2002}),
        service,
    )

    reply = admin.watch(42, ["-1003"])

    assert "Watch added: -1003 -> -2002" in reply
    assert store.list_watches()[0].destination_id == -2002


def test_watch_requires_destination_when_env_missing(monkeypatch, store, registry):
    config = make_config(monkeypatch, BOT_ADMIN_IDS="42")
    admin = AdminCommands(
        config,
        store,
        registry,
        FakeTdlib({-1003}),
        FakeCopyService(),
    )

    with pytest.raises(CommandError, match="DESTINATION is not configured"):
        admin.watch(42, ["-1003"])


def test_watch_rejects_duplicate_source(commands, store):
    commands.watch(42, ["-1002", "-2002"])

    with pytest.raises(CommandError, match="already exists"):
        commands.watch(42, ["-1002", "-2003"])


def test_unwatch_refreshes_registry(commands, registry, store):
    commands.watch(42, ["-1002", "-2002"])
    assert len(registry.dynamic_tasks) == 1

    reply = commands.unwatch(42, ["-1002"])

    assert "removed" in reply
    assert registry.dynamic_tasks == ()
    assert store.list_watches() == []


def test_lswatch_excludes_builtin_only(registry):
    config = AppConfig(
        phone="+1",
        api_id="1",
        api_hash="hash",
        db_password="pw",
        source_id=-1001,
        destination_id=-2001,
        bot_token=None,
        bot_admin_ids=frozenset({42}),
        files_directory="data/tdlib_files",
        send_copy=True,
        proxy_url=None,
        data_directory=Path("data"),
    )
    admin = AdminCommands(
        config,
        StateStore(":memory:"),
        registry,
        FakeTdlib(),
        FakeCopyService(),
    )

    reply = admin.list_watches(42)

    assert reply == "No dynamic watch tasks configured."


def test_copy_requires_two_arguments(commands):
    with pytest.raises(CommandError, match="Usage: /copy"):
        commands.copy(42, 99, ["-1002"])


def test_copy_queues_job_and_returns_job_id(monkeypatch, store, registry):
    config = make_config(monkeypatch, BOT_ADMIN_IDS="42")
    service = FakeCopyService()
    admin = AdminCommands(
        config,
        store,
        registry,
        FakeTdlib({-1002, -2003}),
        service,
    )

    reply = admin.copy(42, 99, ["-1002", "-2003"])

    assert "Copy job 1 queued" in reply
    assert service.jobs[0][0] == Route(-1002, -2003)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_admin_bot_disabled_without_token(monkeypatch, commands):
    config = make_config(monkeypatch, BOT_TOKEN=None, BOT_ADMIN_IDS="42")
    bot = AdminBot(config, commands)

    status = bot.start()

    assert status.enabled is False
    assert "BOT_TOKEN" in status.reason


def test_admin_bot_rejects_non_private_chat(monkeypatch, store, registry):
    config = make_config(
        monkeypatch,
        BOT_TOKEN="token",
        BOT_ADMIN_IDS="42",
    )
    sent_messages = []

    def opener(request, timeout=None):
        url = request.full_url
        if "getUpdates" in url:
            return FakeResponse(
                {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "message_id": 1,
                                "from": {"id": 42},
                                "chat": {"id": -999, "type": "group"},
                                "text": "/lswatch",
                            },
                        }
                    ],
                }
            )
        if "sendMessage" in url:
            sent_messages.append(request.full_url)
            return FakeResponse({"ok": True, "result": {}})
        raise AssertionError(url)

    bot = AdminBot(config, AdminCommands(
        config,
        store,
        registry,
        FakeTdlib(),
        FakeCopyService(),
    ), opener=opener)

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self._target = target

        def start(self):
            self._target()

        def join(self, timeout=None):
            return None

    bot._thread_factory = ImmediateThread
    bot._fetch_updates = lambda: [
        {
            "update_id": 1,
            "message": {
                "from": {"id": 42},
                "chat": {"id": -999, "type": "group"},
                "text": "/lswatch",
            },
        }
    ]
    bot._stop_event.set()
    bot._handle_update(
        {
            "message": {
                "from": {"id": 42},
                "chat": {"id": -999, "type": "group"},
                "text": "/lswatch",
            }
        }
    )

    assert sent_messages == []


def test_api_request_passes_numeric_timeout(monkeypatch, store, registry):
    config = make_config(
        monkeypatch,
        BOT_TOKEN="token",
        BOT_ADMIN_IDS="42",
    )
    timeouts = []

    def opener(request, timeout=None):
        timeouts.append(timeout)
        return FakeResponse({"ok": True, "result": []})

    bot = AdminBot(
        config,
        AdminCommands(
            config,
            store,
            registry,
            FakeTdlib(),
            FakeCopyService(),
        ),
        opener=opener,
    )

    bot._api_request("getUpdates", {"offset": 0, "timeout": 30})

    assert timeouts == [35]
    assert isinstance(timeouts[0], int)


def test_poll_loop_treats_remote_disconnect_as_network_warning(
    monkeypatch,
    store,
    registry,
    caplog,
):
    config = make_config(
        monkeypatch,
        BOT_TOKEN="token",
        BOT_ADMIN_IDS="42",
    )

    def opener(request, timeout=None):
        raise http.client.RemoteDisconnected(
            "Remote end closed connection without response"
        )

    bot = AdminBot(
        config,
        AdminCommands(
            config,
            store,
            registry,
            FakeTdlib(),
            FakeCopyService(),
        ),
        opener=opener,
    )
    bot._wait = lambda seconds: (bot._stop_event.set() or True)

    with caplog.at_level(logging.WARNING):
        bot._poll_loop()

    assert any("Bot API network error" in record.message for record in caplog.records)
    assert not any(
        "Bot polling failed unexpectedly" in record.message
        for record in caplog.records
    )


def test_build_http_opener_without_proxy_uses_urlopen():
    from telecopy.admin_bot import build_http_opener
    import urllib.request

    opener = build_http_opener(None)
    assert opener is urllib.request.urlopen


def test_build_http_opener_with_proxy_uses_socks5(monkeypatch):
    from telecopy import admin_bot

    captured = {}

    class FakeHandler:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    class FakeOpener:
        def open(self, request, timeout=None):
            return FakeResponse({"ok": True, "result": []})

    monkeypatch.setattr(admin_bot, "SocksiPyHandler", FakeHandler)
    monkeypatch.setattr(
        admin_bot.urllib.request,
        "build_opener",
        lambda *handlers: FakeOpener(),
    )

    opener = admin_bot.build_http_opener(
        "socks5://Clash:secret@192.168.0.2:7891"
    )
    assert opener is not urllib.request.urlopen

    response = opener(
        urllib.request.Request("https://api.telegram.org"),
        timeout=35,
    )
    assert response.read()

    assert captured["args"][0] == admin_bot.socks.SOCKS5
    assert captured["args"][1] == "192.168.0.2"
    assert captured["args"][2] == 7891
    assert captured["args"][3] is True
    assert captured["args"][4] == "Clash"
    assert captured["args"][5] == "secret"


def test_build_admin_bot_uses_proxy_opener(monkeypatch, store, registry):
    from telecopy.admin_bot import build_admin_bot

    config = make_config(
        monkeypatch,
        BOT_TOKEN="token",
        BOT_ADMIN_IDS="42",
        PROXY_URL="socks5://user:pass@127.0.0.1:1080",
    )
    sentinel = object()
    monkeypatch.setattr(
        "telecopy.admin_bot.build_http_opener",
        lambda proxy_url: sentinel if proxy_url else urllib.request.urlopen,
    )

    bot = build_admin_bot(
        config,
        store,
        registry,
        FakeTdlib(),
        FakeCopyService(),
    )

    assert bot._opener is sentinel
