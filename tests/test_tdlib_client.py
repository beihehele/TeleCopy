from pathlib import Path
import traceback

import pytest
from telegram.utils import AsyncResult as RealAsyncResult

from telecopy.config import AppConfig
from telecopy.tdlib_client import (
    ProxyUrlError,
    TdlibClient,
    TdlibResponseError,
    TdlibStateError,
    _default_telegram_factory,
    parse_proxy_url,
)


class FakeAsyncResult:
    def __init__(self, update=None, error_info=None):
        self.update = update
        self.error = error_info is not None
        self.error_info = error_info
        self.wait_count = 0
        self.wait_raise_exc_values = []

    def wait(self, timeout=None, raise_exc=False):
        self.wait_count += 1
        self.wait_raise_exc_values.append(raise_exc)
        if raise_exc and self.error:
            raise RuntimeError(f"Telegram error: {self.error_info}")


class FakeTelegram:
    def __init__(self, login_errors=None, stop_errors=None, **kwargs):
        self.constructor_kwargs = kwargs
        self.login_count = 0
        self.stop_count = 0
        self.login_errors = list(login_errors or [])
        self.stop_errors = list(stop_errors or [])
        self.added_handlers = []
        self.removed_handlers = []
        self.chat_results = {}
        self.history_results = []
        self.history_calls = []
        self.method_calls = []
        self.forward_result = FakeAsyncResult(
            {"@type": "messages", "messages": [{"id": 9001}]}
        )

    def login(self):
        self.login_count += 1
        if self.login_errors:
            raise self.login_errors.pop(0)

    def stop(self):
        self.stop_count += 1
        if self.stop_errors:
            raise self.stop_errors.pop(0)

    def add_update_handler(self, handler_type, handler):
        self.added_handlers.append((handler_type, handler))

    def remove_update_handler(self, handler_type, handler):
        self.removed_handlers.append((handler_type, handler))

    def get_chat(self, chat_id):
        return self.chat_results[chat_id]

    def get_chat_history(
        self,
        chat_id,
        limit=100,
        from_message_id=0,
    ):
        self.history_calls.append(
            {
                "chat_id": chat_id,
                "limit": limit,
                "from_message_id": from_message_id,
            }
        )
        return self.history_results.pop(0)

    def call_method(self, method_name, params, block=False):
        self.method_calls.append((method_name, params, block))
        if block:
            self.forward_result.wait(raise_exc=True)
        return self.forward_result


@pytest.fixture
def config():
    return AppConfig(
        phone="+15551234567",
        api_id="12345",
        api_hash="api-hash-secret",
        db_password="database-password-secret",
        source_id=None,
        destination_id=None,
        bot_token=None,
        bot_admin_ids=frozenset(),
        files_directory="data/tdlib_files",
        send_copy=True,
        proxy_url="socks5://user:p%40ss@proxy.example:1080",
        data_directory=Path("data"),
    )


@pytest.fixture
def telegram_factory():
    instances = []

    def create(**kwargs):
        instance = FakeTelegram(
            login_errors=create.login_errors,
            stop_errors=create.stop_errors,
            **kwargs,
        )
        instances.append(instance)
        return instance

    create.instances = instances
    create.login_errors = []
    create.stop_errors = []
    return create


def test_parse_authenticated_socks5_url_decodes_credentials():
    proxy = parse_proxy_url("socks5://us%40er:p%40ss@host:1080")

    assert proxy.server == "host"
    assert proxy.port == 1080
    assert proxy.type == {
        "@type": "proxyTypeSocks5",
        "username": "us@er",
        "password": "p@ss",
    }


@pytest.mark.parametrize(
    ("url", "server"),
    [
        ("socks5://host:1080", "host"),
        ("socks5://proxy.example.com:1080", "proxy.example.com"),
        ("socks5://proxy%2Eexample.com:1080", "proxy.example.com"),
        ("socks5://192.0.2.10:1080", "192.0.2.10"),
        ("socks5://localhost:1080", "localhost"),
        ("socks5://münich.example:1080", "xn--mnich-kva.example"),
    ],
)
def test_parse_socks5_url_accepts_common_hosts(url, server):
    proxy = parse_proxy_url(url)

    assert proxy.server == server
    assert proxy.type == {"@type": "proxyTypeSocks5"}


def test_parse_socks5_url_accepts_ipv6_host():
    proxy = parse_proxy_url("socks5://user:pass@[2001:db8::1]:1080")

    assert proxy.server == "2001:db8::1"
    assert proxy.port == 1080


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://proxy.example:8080", "socks5"),
        ("socks5://:1080", "host"),
        ("socks5://proxy.example", "port"),
        ("socks5://proxy.example:0", "port"),
        ("socks5://proxy.example:not-a-port", "port"),
        ("socks5://proxy.example:70000", "port"),
        ("socks5://proxy.example:1080/path", "path"),
        ("socks5://proxy.example:1080?option=value", "query"),
        ("socks5://proxy.example:1080#fragment", "fragment"),
        ("socks5://bad host:1080", "host"),
        ("socks5://bad%20host:1080", "host"),
        ("socks5://bad%0Ahost:1080", "host"),
        ("socks5://bad\x01host:1080", "host"),
        ("socks5://bad%zzhost:1080", "host"),
        ("socks5://bad%host:1080", "host"),
        ("socks5://bad!host:1080", "host"),
        ("socks5://host%2Fpath:1080", "host"),
        ("socks5://.example.com:1080", "host"),
        ("socks5://example.com.:1080", "host"),
        ("socks5://example..com:1080", "host"),
        ("socks5://-example.com:1080", "host"),
        ("socks5://example-.com:1080", "host"),
        (f"socks5://{'a' * 64}.example:1080", "host"),
        (
            "socks5://"
            + ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 62])
            + ":1080",
            "host",
        ),
    ],
)
def test_parse_proxy_url_rejects_invalid_values_without_leaking_url(
    url, message
):
    with pytest.raises(ProxyUrlError, match=message) as error:
        parse_proxy_url(url)

    assert url not in str(error.value)


def test_proxy_repr_hides_authentication_values():
    proxy = parse_proxy_url(
        "socks5://sentinel-user:sentinel-password@host:1080"
    )

    proxy_repr = repr(proxy)

    assert "sentinel-user" not in proxy_repr
    assert "sentinel-password" not in proxy_repr


def test_default_factory_imports_locked_python_telegram_client(monkeypatch):
    calls = []
    sentinel = object()

    def fake_telegram(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr("telegram.client.Telegram", fake_telegram)

    result = _default_telegram_factory(api_id=12345)

    assert result is sentinel
    assert calls == [{"api_id": 12345}]


def test_real_async_result_stores_errors_outside_update():
    result = RealAsyncResult(client=object())
    error_info = {
        "@type": "error",
        "code": 429,
        "message": "FLOOD_WAIT_7",
    }

    result.parse_update(error_info)

    assert result.error is True
    assert result.error_info == error_info
    assert result.update is None


def test_connect_constructs_telegram_with_config_and_logs_in(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)

    client.connect()

    telegram = telegram_factory.instances[0]
    assert telegram.constructor_kwargs == {
        "api_id": 12345,
        "api_hash": "api-hash-secret",
        "phone": "+15551234567",
        "database_encryption_key": "database-password-secret",
        "files_directory": "data/tdlib_files",
        "proxy_server": "proxy.example",
        "proxy_port": 1080,
        "proxy_type": {
            "@type": "proxyTypeSocks5",
            "username": "user",
            "password": "p@ss",
        },
    }
    assert telegram.login_count == 1


def test_connect_without_proxy_uses_python_telegram_defaults(
    config, telegram_factory
):
    client = TdlibClient(
        AppConfig(
            **{
                **config.__dict__,
                "proxy_url": None,
            }
        ),
        telegram_factory=telegram_factory,
    )

    client.connect()

    kwargs = telegram_factory.instances[0].constructor_kwargs
    assert kwargs["proxy_server"] == ""
    assert kwargs["proxy_port"] == 0
    assert kwargs["proxy_type"] is None


def test_connect_is_idempotent(config, telegram_factory):
    client = TdlibClient(config, telegram_factory=telegram_factory)

    client.connect()
    client.connect()

    assert len(telegram_factory.instances) == 1
    assert telegram_factory.instances[0].login_count == 1


def test_login_failure_is_not_masked_when_cleanup_fails(
    config, telegram_factory
):
    telegram_factory.login_errors = [RuntimeError("original login failure")]
    telegram_factory.stop_errors = [RuntimeError("secondary cleanup failure")]
    client = TdlibClient(config, telegram_factory=telegram_factory)

    with pytest.raises(RuntimeError, match="original login failure"):
        client.connect()

    telegram = telegram_factory.instances[0]
    assert telegram.login_count == 1
    assert telegram.stop_count == 1
    with pytest.raises(TdlibStateError):
        client.validate_chat(-1001)

    client.stop()

    assert telegram.stop_count == 2


def test_stop_is_idempotent(config, telegram_factory):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()

    client.stop()
    client.stop()

    assert telegram_factory.instances[0].stop_count == 1


def test_stop_failure_keeps_client_available_for_cleanup_retry(
    config, telegram_factory
):
    telegram_factory.stop_errors = [RuntimeError("temporary stop failure")]
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()

    with pytest.raises(RuntimeError, match="temporary stop failure"):
        client.stop()

    with pytest.raises(TdlibStateError):
        client.add_new_message_handler(lambda update: None)

    client.stop()
    client.stop()

    assert telegram_factory.instances[0].stop_count == 2


def test_new_message_handler_uses_python_telegram_signature(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    handler = lambda update: None

    client.add_new_message_handler(handler)
    client.remove_new_message_handler(handler)

    telegram = telegram_factory.instances[0]
    assert telegram.added_handlers == [("updateNewMessage", handler)]
    assert telegram.removed_handlers == [("updateNewMessage", handler)]


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"@type": "chat", "id": -1001, "title": "Source"}, True),
        ({"@type": "error", "code": 400, "message": "not found"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_validate_chat_converts_tdlib_result_to_boolean(
    config, telegram_factory, update, expected
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram = telegram_factory.instances[0]
    result = FakeAsyncResult(update)
    telegram.chat_results[-1001] = result

    assert client.validate_chat(-1001) is expected
    assert result.wait_count == 1


def test_iter_history_yields_unique_messages_and_advances_cursor(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram = telegram_factory.instances[0]
    telegram.history_results = [
        FakeAsyncResult(
            {"@type": "messages", "messages": [{"id": 3}, {"id": 2}]}
        ),
        FakeAsyncResult(
            {"@type": "messages", "messages": [{"id": 2}, {"id": 1}]}
        ),
        FakeAsyncResult({"@type": "messages", "messages": []}),
    ]

    messages = list(client.iter_history(-1001))

    assert [message["id"] for message in messages] == [3, 2, 1]
    assert telegram.history_calls == [
        {"chat_id": -1001, "limit": 100, "from_message_id": 0},
        {"chat_id": -1001, "limit": 100, "from_message_id": 2},
        {"chat_id": -1001, "limit": 100, "from_message_id": 1},
    ]


def test_iter_history_converts_real_async_result_error_to_domain_error(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram_factory.instances[0].history_results = [
        FakeAsyncResult(
            error_info={
                "@type": "error",
                "code": 429,
                "message": "FLOOD_WAIT_7",
            }
        )
    ]

    with pytest.raises(TdlibResponseError) as error:
        list(client.iter_history(-1001))

    assert error.value.code == 429
    assert error.value.tdlib_message == "FLOOD_WAIT_7"


@pytest.mark.parametrize("update", [[], "not-a-dictionary", 1])
def test_iter_history_rejects_non_dictionary_update(
    config, telegram_factory, update
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram_factory.instances[0].history_results = [
        FakeAsyncResult(update)
    ]

    with pytest.raises(TdlibResponseError, match="malformed history"):
        list(client.iter_history(-1001))


def test_iter_history_stops_when_pagination_stalls(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram = telegram_factory.instances[0]
    telegram.history_results = [
        FakeAsyncResult(
            {"@type": "messages", "messages": [{"id": 2}, {"id": 1}]}
        ),
        FakeAsyncResult(
            {"@type": "messages", "messages": [{"id": 2}, {"id": 1}]}
        ),
    ]

    assert [message["id"] for message in client.iter_history(-1001)] == [2, 1]
    assert len(telegram.history_calls) == 2


def test_iter_history_stops_when_cursor_moves_toward_newer_messages(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram = telegram_factory.instances[0]
    telegram.history_results = [
        FakeAsyncResult(
            {"@type": "messages", "messages": [{"id": 3}, {"id": 2}]}
        ),
        FakeAsyncResult(
            {"@type": "messages", "messages": [{"id": 2}, {"id": 3}]}
        ),
    ]

    assert [message["id"] for message in client.iter_history(-1001)] == [3, 2]
    assert len(telegram.history_calls) == 2


def test_iter_history_keeps_only_page_boundary_state_for_long_history(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram = telegram_factory.instances[0]
    telegram.history_results = [
        FakeAsyncResult(
            {
                "@type": "messages",
                "messages": [{"id": message_id}, {"id": message_id - 1}],
            }
        )
        for message_id in range(1000, 700, -1)
    ]
    telegram.history_results.append(
        FakeAsyncResult({"@type": "messages", "messages": []})
    )

    history = client.iter_history(-1001)
    first_messages = [next(history) for _ in range(50)]
    retained_sets = [
        value
        for value in history.gi_frame.f_locals.values()
        if isinstance(value, set)
    ]
    remaining_messages = list(history)

    assert retained_sets == []
    assert [message["id"] for message in first_messages] == list(
        range(1000, 950, -1)
    )
    assert [message["id"] for message in remaining_messages] == list(
        range(950, 699, -1)
    )


@pytest.mark.parametrize(
    "message",
    [
        None,
        {"id": "not-an-integer"},
        {"id": True},
        {"id": 0},
        {"id": -1},
    ],
)
def test_iter_history_converts_malformed_items_to_domain_error(
    config, telegram_factory, message
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram_factory.instances[0].history_results = [
        FakeAsyncResult({"@type": "messages", "messages": [message]})
    ]

    with pytest.raises(TdlibResponseError, match="invalid message"):
        list(client.iter_history(-1001))

    assert len(telegram_factory.instances[0].history_calls) == 1


def test_forward_message_calls_forward_messages_and_returns_target_id(
    config, telegram_factory
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()

    target_id = client.forward_message(-1001, -2001, 42, send_copy=False)

    telegram = telegram_factory.instances[0]
    assert target_id == 9001
    assert telegram.method_calls == [
        (
            "forwardMessages",
            {
                "chat_id": -2001,
                "from_chat_id": -1001,
                "message_ids": [42],
                "send_copy": False,
            },
            False,
        )
    ]
    assert telegram.forward_result.wait_count == 1
    assert telegram.forward_result.wait_raise_exc_values == [False]


def test_fake_telegram_models_blocking_error_behavior():
    telegram = FakeTelegram()
    telegram.forward_result = FakeAsyncResult(
        error_info={
            "@type": "error",
            "code": 429,
            "message": "FLOOD_WAIT_2",
        }
    )

    with pytest.raises(RuntimeError, match="Telegram error"):
        telegram.call_method("forwardMessages", {}, block=True)


def test_forward_message_before_connect_preserves_state_error(config):
    client = TdlibClient(config, telegram_factory=lambda **kwargs: None)

    with pytest.raises(TdlibStateError):
        client.forward_message(-1001, -2001, 42, send_copy=True)


@pytest.mark.parametrize(
    ("update", "message"),
    [
        (None, "empty response"),
        ({"@type": "messages", "messages": []}, "no forwarded message"),
        ({"@type": "messages", "messages": [None]}, "message id"),
        (
            {"@type": "messages", "messages": [{"chat_id": -2001}]},
            "message id",
        ),
    ],
)
def test_forward_message_converts_invalid_responses_to_domain_error(
    config, telegram_factory, update, message
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram_factory.instances[0].forward_result = FakeAsyncResult(update)

    with pytest.raises(TdlibResponseError, match=message):
        client.forward_message(-1001, -2001, 42, send_copy=True)


@pytest.mark.parametrize("target_id", [0, -1, True])
def test_forward_message_rejects_invalid_target_message_id(
    config, telegram_factory, target_id
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram_factory.instances[0].forward_result = FakeAsyncResult(
        {"@type": "messages", "messages": [{"id": target_id}]}
    )

    with pytest.raises(TdlibResponseError, match="message id"):
        client.forward_message(-1001, -2001, 42, send_copy=True)


def test_forward_message_converts_real_async_result_error_to_domain_error(
    config, telegram_factory
):
    sensitive_value = "sentinel-sensitive-detail"
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram_factory.instances[0].forward_result = FakeAsyncResult(
        error_info={
            "@type": "error",
            "code": 429,
            "message": f"FLOOD_WAIT_2 {sensitive_value}",
        }
    )

    with pytest.raises(TdlibResponseError) as error:
        client.forward_message(-1001, -2001, 42, send_copy=True)

    assert error.value.code == 429
    assert error.value.tdlib_message == f"FLOOD_WAIT_2 {sensitive_value}"
    assert sensitive_value not in str(error.value)
    assert sensitive_value not in repr(error.value)


@pytest.mark.parametrize("update", [[], "not-a-dictionary", 1])
def test_forward_message_rejects_non_dictionary_update(
    config, telegram_factory, update
):
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram_factory.instances[0].forward_result = FakeAsyncResult(update)

    with pytest.raises(TdlibResponseError, match="malformed forwarding"):
        client.forward_message(-1001, -2001, 42, send_copy=True)


def test_forward_message_sanitizes_lower_level_exception(
    config, telegram_factory
):
    sensitive_value = "sentinel-proxy-password"
    client = TdlibClient(config, telegram_factory=telegram_factory)
    client.connect()
    telegram = telegram_factory.instances[0]

    def fail_call_method(method_name, params, block=False):
        raise RuntimeError(f"transport failed with {sensitive_value}")

    telegram.call_method = fail_call_method

    with pytest.raises(TdlibResponseError) as error:
        client.forward_message(-1001, -2001, 42, send_copy=True)

    formatted_error = "".join(
        traceback.format_exception(
            error.type,
            error.value,
            error.tb,
        )
    )
    assert sensitive_value not in str(error.value)
    assert sensitive_value not in repr(error.value)
    assert sensitive_value not in formatted_error
    assert error.value.__suppress_context__ is True
