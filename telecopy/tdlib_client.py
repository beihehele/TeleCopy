"""Small, synchronous adapter around python-telegram's TDLib client.

TDLib invokes registered update handlers on its worker threads. This adapter
does not create threads, schedule retries, or retain routing/database state.
Callers own business-level synchronization and forwarding policy.
"""

import ipaddress
import string
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

from telecopy.config import AppConfig


NEW_MESSAGE_UPDATE = "updateNewMessage"
HISTORY_PAGE_SIZE = 100


class TdlibClientError(RuntimeError):
    """Base exception for TDLib adapter failures."""


class TdlibStateError(TdlibClientError):
    """Raised when an operation requires an active TDLib client."""


class TdlibResponseError(TdlibClientError):
    """Raised when TDLib returns an error or malformed response."""

    def __init__(
        self,
        summary: str,
        *,
        code: int | None = None,
        tdlib_message: str | None = None,
    ) -> None:
        super().__init__(summary)
        self.code = code
        self.tdlib_message = tdlib_message


class ProxyUrlError(TdlibClientError, ValueError):
    """Raised when a proxy URL cannot be represented as a TDLib proxy."""


@dataclass(frozen=True)
class ProxySettings:
    """Validated TDLib SOCKS5 settings with hidden authentication data."""

    server: str
    port: int
    type: dict[str, str] = field(repr=False)


class AsyncResult(Protocol):
    error: bool
    error_info: dict[str, Any] | None
    update: dict[str, Any] | None

    def wait(
        self,
        timeout: int | None = None,
        raise_exc: bool = False,
    ) -> Any:
        """Wait until TDLib supplies an update."""


class TelegramClient(Protocol):
    def login(self) -> Any: ...

    def stop(self) -> None: ...

    def add_update_handler(
        self,
        handler_type: str,
        handler: Callable[[dict], None],
    ) -> None: ...

    def remove_update_handler(
        self,
        handler_type: str,
        handler: Callable[[dict], None],
    ) -> None: ...

    def get_chat(self, chat_id: int) -> AsyncResult: ...

    def get_chat_history(
        self,
        chat_id: int,
        limit: int = 100,
        from_message_id: int = 0,
    ) -> AsyncResult: ...

    def call_method(
        self,
        method_name: str,
        params: dict[str, Any],
        block: bool = False,
    ) -> AsyncResult: ...


TelegramFactory = Callable[..., TelegramClient]


def parse_proxy_url(proxy_url: str) -> ProxySettings:
    """Parse a SOCKS5 URL without exposing its value in validation errors."""

    if any(ord(character) < 32 or ord(character) == 127 for character in proxy_url):
        raise ProxyUrlError("SOCKS5 proxy host is invalid")

    try:
        parsed = urlparse(proxy_url)
    except ValueError:
        raise ProxyUrlError("Invalid SOCKS5 proxy URL") from None

    if parsed.scheme.casefold() != "socks5":
        raise ProxyUrlError("Proxy scheme must be socks5")
    if parsed.path:
        raise ProxyUrlError("SOCKS5 proxy path is not allowed")
    if parsed.query:
        raise ProxyUrlError("SOCKS5 proxy query is not allowed")
    if parsed.fragment:
        raise ProxyUrlError("SOCKS5 proxy fragment is not allowed")

    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ProxyUrlError("SOCKS5 proxy port is invalid") from None

    if not hostname:
        raise ProxyUrlError("SOCKS5 proxy host is required")
    hostname = _decode_proxy_hostname(hostname)
    if port is None or not 1 <= port <= 65535:
        raise ProxyUrlError("SOCKS5 proxy port must be between 1 and 65535")

    proxy_type = {"@type": "proxyTypeSocks5"}
    username = unquote(parsed.username) if parsed.username is not None else ""
    password = unquote(parsed.password) if parsed.password is not None else ""
    if username or password:
        proxy_type["username"] = username
        proxy_type["password"] = password

    return ProxySettings(hostname, port, proxy_type)


def _default_telegram_factory(**kwargs: Any) -> TelegramClient:
    # Imported lazily so tests and non-TDLib tooling need not load libtdjson.
    from telegram.client import Telegram

    return Telegram(**kwargs)


class TdlibClient:
    """Synchronous transport adapter for one TDLib user session."""

    def __init__(
        self,
        config: AppConfig,
        telegram_factory: TelegramFactory | None = None,
    ) -> None:
        self._config = config
        self._telegram_factory = telegram_factory or _default_telegram_factory
        self._telegram: TelegramClient | None = None
        self._connected = False
        self._lifecycle_lock = RLock()

    def connect(self) -> None:
        """Create and authorize the client once."""

        with self._lifecycle_lock:
            if self._connected:
                return
            if self._telegram is not None:
                self._stop_locked()

            proxy_server = ""
            proxy_port = 0
            proxy_type = None
            if self._config.proxy_url is not None:
                proxy = parse_proxy_url(self._config.proxy_url)
                proxy_server = proxy.server
                proxy_port = proxy.port
                proxy_type = proxy.type

            telegram = self._telegram_factory(
                api_id=int(self._config.api_id),
                api_hash=self._config.api_hash,
                phone=self._config.phone,
                database_encryption_key=self._config.db_password,
                files_directory=self._config.files_directory,
                proxy_server=proxy_server,
                proxy_port=proxy_port,
                proxy_type=proxy_type,
            )
            self._telegram = telegram
            try:
                telegram.login()
            except BaseException:
                try:
                    self._stop_locked()
                except BaseException:
                    pass
                raise
            self._connected = True

    def stop(self) -> None:
        """Stop the current client once."""

        with self._lifecycle_lock:
            self._stop_locked()

    def add_new_message_handler(
        self,
        handler: Callable[[dict], None],
    ) -> None:
        self._require_telegram().add_update_handler(
            NEW_MESSAGE_UPDATE,
            handler,
        )

    def remove_new_message_handler(
        self,
        handler: Callable[[dict], None],
    ) -> None:
        self._require_telegram().remove_update_handler(
            NEW_MESSAGE_UPDATE,
            handler,
        )

    def validate_chat(self, chat_id: int) -> bool:
        result = self._require_telegram().get_chat(chat_id)
        result.wait()
        update = result.update
        return update is not None and update.get("@type") == "chat"

    def iter_history(self, chat_id: int) -> Iterator[dict]:
        """Yield history newest-first and stop if TDLib pagination stalls."""

        telegram = self._require_telegram()
        from_message_id = 0

        while True:
            result = telegram.get_chat_history(
                chat_id,
                limit=HISTORY_PAGE_SIZE,
                from_message_id=from_message_id,
            )
            result.wait()
            if result.error:
                raise _result_error("history", result.error_info)
            update = result.update
            if update is None:
                raise TdlibResponseError(
                    "TDLib returned an empty history response"
                )
            if not isinstance(update, dict):
                raise TdlibResponseError(
                    "TDLib returned a malformed history response"
                )
            if update.get("@type") == "error":
                raise _result_error("history", update)

            messages = update.get("messages")
            if not isinstance(messages, list):
                raise TdlibResponseError(
                    "TDLib history response has no message list"
                )
            if not messages:
                return

            for message in messages:
                if not isinstance(message, dict):
                    raise TdlibResponseError(
                        "TDLib history response has an invalid message"
                    )
                message_id = message.get("id")
                if type(message_id) is not int or message_id <= 0:
                    raise TdlibResponseError(
                        "TDLib history response has an invalid message id"
                    )

            next_message_id = messages[-1]["id"]
            if (
                from_message_id != 0
                and next_message_id >= from_message_id
            ):
                return

            for message in messages:
                if message["id"] != from_message_id:
                    yield message

            from_message_id = next_message_id

    def forward_message(
        self,
        source_id: int,
        destination_id: int,
        message_id: int,
        send_copy: bool,
    ) -> int:
        """Forward once; retry and FloodWait policy belongs to the caller."""

        telegram = self._require_telegram()
        try:
            result = telegram.call_method(
                "forwardMessages",
                {
                    "chat_id": destination_id,
                    "from_chat_id": source_id,
                    "message_ids": [message_id],
                    "send_copy": send_copy,
                },
                block=False,
            )
            result.wait(raise_exc=False)
        except Exception:
            raise TdlibResponseError(
                "TDLib forwarding request failed"
            ) from None
        if result.error:
            raise _result_error("forwarding", result.error_info)
        update = result.update
        if update is None:
            raise TdlibResponseError(
                "TDLib returned an empty response while forwarding"
            )
        if not isinstance(update, dict):
            raise TdlibResponseError(
                "TDLib returned a malformed forwarding response"
            )
        if update.get("@type") == "error":
            raise _result_error("forwarding", update)

        messages = update.get("messages")
        if not isinstance(messages, list) or not messages:
            raise TdlibResponseError(
                "TDLib forwarding response contains no forwarded message"
            )
        first_message = messages[0]
        target_id = (
            first_message.get("id")
            if isinstance(first_message, dict)
            else None
        )
        if type(target_id) is not int or target_id <= 0:
            raise TdlibResponseError(
                "TDLib forwarding response contains no message id"
            )
        return target_id

    def _require_telegram(self) -> TelegramClient:
        with self._lifecycle_lock:
            if self._telegram is None or not self._connected:
                raise TdlibStateError("TDLib client is not connected")
            return self._telegram

    def _stop_locked(self) -> None:
        telegram = self._telegram
        self._connected = False
        if telegram is None:
            return
        telegram.stop()
        self._telegram = None


def _decode_proxy_hostname(hostname: str) -> str:
    index = 0
    while index < len(hostname):
        if hostname[index] != "%":
            index += 1
            continue
        escape = hostname[index + 1:index + 3]
        if (
            len(escape) != 2
            or any(character not in string.hexdigits for character in escape)
        ):
            raise ProxyUrlError("SOCKS5 proxy host has invalid percent encoding")
        index += 3

    decoded = unquote(hostname)
    if any(
        character.isspace()
        or ord(character) < 32
        or ord(character) == 127
        for character in decoded
    ):
        raise ProxyUrlError("SOCKS5 proxy host is invalid")

    try:
        ipaddress.ip_address(decoded)
    except ValueError:
        pass
    else:
        return decoded

    try:
        ascii_hostname = decoded.encode("idna").decode("ascii")
    except UnicodeError:
        raise ProxyUrlError("SOCKS5 proxy host is invalid") from None

    if (
        not ascii_hostname
        or len(ascii_hostname) > 253
        or ascii_hostname.startswith(".")
        or ascii_hostname.endswith(".")
    ):
        raise ProxyUrlError("SOCKS5 proxy host is invalid")

    for label in ascii_hostname.split("."):
        if (
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or any(
                character not in string.ascii_letters
                + string.digits
                + "-"
                for character in label
            )
        ):
            raise ProxyUrlError("SOCKS5 proxy host is invalid")

    return ascii_hostname.lower()


def _result_error(
    operation: str,
    error_info: dict[str, Any] | None,
) -> TdlibResponseError:
    code = None
    tdlib_message = None
    if isinstance(error_info, dict):
        raw_code = error_info.get("code")
        raw_message = error_info.get("message")
        if type(raw_code) is int:
            code = raw_code
        if isinstance(raw_message, str):
            tdlib_message = raw_message

    summary = f"TDLib {operation} failed"
    if code is not None:
        summary += f" with error code {code}"
    return TdlibResponseError(
        summary,
        code=code,
        tdlib_message=tdlib_message,
    )
