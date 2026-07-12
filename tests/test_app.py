from pathlib import Path
import signal

import pytest

from telecopy.app import TeleCopyApplication, clear_session_files
from telecopy.config import AppConfig, Route
from telecopy.database import StateStore


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


def make_config(monkeypatch, tmp_path, **overrides):
    set_required_env(monkeypatch)
    for key, value in overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, str(value))
    config = AppConfig.from_env()
    return AppConfig(
        phone=config.phone,
        api_id=config.api_id,
        api_hash=config.api_hash,
        db_password=config.db_password,
        source_id=overrides.get("SOURCE", config.source_id),
        destination_id=overrides.get("DESTINATION", config.destination_id),
        bot_token=config.bot_token,
        bot_admin_ids=config.bot_admin_ids,
        files_directory=str(tmp_path / "tdlib_files"),
        send_copy=config.send_copy,
        proxy_url=config.proxy_url,
        data_directory=tmp_path / "data",
    )


class FakeTdlib:
    def __init__(self):
        self.connected = False
        self.stopped = False
        self.handlers = []

    def connect(self):
        self.connected = True

    def stop(self):
        self.stopped = True
        self.connected = False

    def add_new_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_new_message_handler(self, handler):
        self.handlers.remove(handler)

    def validate_chat(self, chat_id):
        return True

    def iter_history(self, chat_id):
        return iter(())

    def forward_message(self, source_id, destination_id, message_id, send_copy):
        return message_id + 1


class FakeMonitor:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeCopyService:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self, timeout_seconds=10.0):
        self.stopped = True
        return True


class FakeAdminBot:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.started = False
        self.stopped = False

    def start(self):
        status = type("Status", (), {"enabled": self.enabled, "reason": None})()
        if self.enabled:
            self.started = True
        return status

    def stop(self, timeout_seconds=5.0):
        self.stopped = True


def build_app(monkeypatch, tmp_path, **overrides):
    config = make_config(monkeypatch, tmp_path, **overrides)
    store = StateStore(config.data_directory / "telecopy.db")
    registry = __import__("telecopy.tasks", fromlist=["RouteRegistry"]).RouteRegistry(
        config.builtin_route
    )
    tdlib = FakeTdlib()
    copy_service = FakeCopyService()
    monitor = FakeMonitor()
    admin_bot = FakeAdminBot(enabled=config.bot_token is not None)
    return TeleCopyApplication(
        config,
        store=store,
        tdlib=tdlib,
        registry=registry,
        copy_service=copy_service,
        monitor=monitor,
        admin_bot=admin_bot,
    )


def test_start_connects_monitor_and_optional_bot(monkeypatch, tmp_path):
    app = build_app(
        monkeypatch,
        tmp_path,
        SOURCE="-1001",
        DESTINATION="-2001",
        BOT_TOKEN="token",
        BOT_ADMIN_IDS="42",
    )

    app.start()

    assert app._tdlib.connected is True
    assert app._copy_service.started is True
    assert app._monitor.started is True
    assert app._admin_bot.started is True


def test_start_without_bot_still_monitors(monkeypatch, tmp_path):
    app = build_app(monkeypatch, tmp_path, BOT_TOKEN=None)

    app.start()

    assert app._monitor.started is True
    assert app._admin_bot.started is False


def test_credential_reset_clears_dynamic_tasks(monkeypatch, tmp_path):
    app1 = build_app(monkeypatch, tmp_path, API_HASH="hash-one")
    app1.start()
    app1._store.add_watch(-1002, -2002, 42)
    assert len(app1._store.list_watches()) == 1
    app1.stop()

    app2 = build_app(monkeypatch, tmp_path, API_HASH="hash-two")
    app2.start()

    assert app2._store.list_watches() == []


def test_stop_shuts_down_in_reverse_order(monkeypatch, tmp_path):
    app = build_app(
        monkeypatch,
        tmp_path,
        BOT_TOKEN="token",
        BOT_ADMIN_IDS="42",
    )
    app.start()
    app.stop()

    assert app._admin_bot.stopped is True
    assert app._monitor.stopped is True
    assert app._copy_service.stopped is True
    assert app._tdlib.stopped is True


def test_clear_session_files_preserves_database(monkeypatch, tmp_path):
    config = make_config(monkeypatch, tmp_path)
    database_path = config.data_directory / "telecopy.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_path.write_text("db", encoding="utf-8")
    files_directory = Path(config.files_directory)
    files_directory.mkdir(parents=True, exist_ok=True)
    (files_directory / "session.bin").write_text("x", encoding="utf-8")
    (config.data_directory / "copy_map.json").write_text("{}", encoding="utf-8")

    clear_session_files(config)

    assert database_path.read_text(encoding="utf-8") == "db"
    assert not (files_directory / "session.bin").exists()
    assert not (config.data_directory / "copy_map.json").exists()
