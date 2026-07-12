from dataclasses import FrozenInstanceError
import logging
from pathlib import Path
import traceback

import pytest

from telecopy.config import AppConfig, ConfigError, Route


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


def test_missing_required_values_are_reported_together(monkeypatch):
    monkeypatch.setenv("PHONE", " ")

    with pytest.raises(ConfigError) as error:
        AppConfig.from_env()

    message = str(error.value)
    assert "PHONE" in message
    assert "API_ID" in message
    assert "API_HASH" in message
    assert "DB_PASSWORD" in message


def test_load_config_allows_empty_builtin_route(monkeypatch):
    set_required_env(monkeypatch)

    config = AppConfig.from_env()

    assert config.builtin_route is None


def test_source_without_destination_warns_and_has_no_builtin_route(
    monkeypatch, caplog
):
    set_required_env(monkeypatch)
    monkeypatch.setenv("SOURCE", "-1001")

    with caplog.at_level(logging.WARNING, logger="telecopy.config"):
        config = AppConfig.from_env()

    assert config.source_id == -1001
    assert config.destination_id is None
    assert config.builtin_route is None
    assert (
        "SOURCE is set but DESTINATION is missing; built-in route disabled"
        in caplog.messages
    )
    assert "-1001" not in caplog.text


def test_destination_without_source_has_no_builtin_route(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("DESTINATION", "-2001")

    config = AppConfig.from_env()

    assert config.source_id is None
    assert config.destination_id == -2001
    assert config.builtin_route is None


def test_complete_builtin_route_is_parsed(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("SOURCE", "-1001")
    monkeypatch.setenv("DESTINATION", "-2001")

    config = AppConfig.from_env()

    assert config.builtin_route == Route(-1001, -2001)


def test_bot_settings_are_optional(monkeypatch):
    set_required_env(monkeypatch)

    config = AppConfig.from_env()

    assert config.bot_token is None
    assert config.bot_admin_ids == frozenset()


def test_bot_admin_ids_parse_comma_separated_integers(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_ADMIN_IDS", "123, 456")

    config = AppConfig.from_env()

    assert config.bot_admin_ids == frozenset({123, 456})


@pytest.mark.parametrize("value", ["abc", "123,,456", "1.5"])
def test_invalid_bot_admin_ids_raise_config_error(monkeypatch, value):
    set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_ADMIN_IDS", value)

    with pytest.raises(ConfigError, match="BOT_ADMIN_IDS"):
        AppConfig.from_env()


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_send_copy_parses_common_true_values(monkeypatch, value):
    set_required_env(monkeypatch)
    monkeypatch.setenv("SEND_COPY", value)

    assert AppConfig.from_env().send_copy is True


@pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off"])
def test_send_copy_parses_common_false_values(monkeypatch, value):
    set_required_env(monkeypatch)
    monkeypatch.setenv("SEND_COPY", value)

    assert AppConfig.from_env().send_copy is False


def test_invalid_send_copy_raises_config_error(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("SEND_COPY", "sometimes")

    with pytest.raises(ConfigError, match="SEND_COPY"):
        AppConfig.from_env()


def test_defaults_and_proxy_url_are_loaded(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("PROXY_URL", "socks5://user:pass@proxy:1080")

    config = AppConfig.from_env()

    assert config.files_directory == "data/tdlib_files"
    assert config.send_copy is True
    assert config.proxy_url == "socks5://user:pass@proxy:1080"
    assert config.data_directory == Path("data")


def test_app_config_repr_hides_sensitive_values(monkeypatch):
    sensitive_values = {
        "PHONE": "sentinel-phone-secret",
        "API_HASH": "sentinel-api-hash-secret",
        "DB_PASSWORD": "sentinel-db-password-secret",
        "BOT_TOKEN": "sentinel-bot-token-secret",
        "PROXY_URL": "socks5://sentinel-proxy-secret@proxy:1080",
    }
    set_required_env(monkeypatch)
    for name, value in sensitive_values.items():
        monkeypatch.setenv(name, value)

    config_repr = repr(AppConfig.from_env())

    for value in sensitive_values.values():
        assert value not in config_repr


@pytest.mark.parametrize(
    ("name", "invalid_value"),
    [
        ("SOURCE", "sentinel-invalid-route-secret"),
        ("BOT_ADMIN_IDS", "sentinel-invalid-admin-secret"),
    ],
)
def test_config_error_suppresses_invalid_value_exception_chain(
    monkeypatch, name, invalid_value
):
    set_required_env(monkeypatch)
    monkeypatch.setenv(name, invalid_value)

    with pytest.raises(ConfigError) as error:
        AppConfig.from_env()

    formatted_error = "".join(
        traceback.format_exception(
            error.type,
            error.value,
            error.tb,
        )
    )
    assert invalid_value not in formatted_error


@pytest.mark.parametrize("name", ["SOURCE", "DESTINATION"])
def test_invalid_route_id_raises_config_error(monkeypatch, name):
    set_required_env(monkeypatch)
    monkeypatch.setenv(name, "not-an-integer")

    with pytest.raises(ConfigError, match=name):
        AppConfig.from_env()


def test_app_config_and_route_are_frozen(monkeypatch):
    set_required_env(monkeypatch)
    config = AppConfig.from_env()
    route = Route(-1001, -2001)

    with pytest.raises(FrozenInstanceError):
        config.phone = "changed"
    with pytest.raises(FrozenInstanceError):
        route.source_id = -3001
