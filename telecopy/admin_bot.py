"""Authorized Telegram Bot API command handling."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from threading import Event, RLock, Thread
from typing import Any, Callable, Protocol
import urllib.error
import urllib.parse
import urllib.request

from telecopy.config import AppConfig, Route
from telecopy.copy_service import (
    CopyService,
    HistoryJobActiveError,
    HistoryJobResult,
    ServiceStoppedError,
)
from telecopy.database import (
    ActiveCopyJobError,
    DuplicateWatchError,
    StateStore,
)
from telecopy.tasks import RouteRegistry
from telecopy.tdlib_client import TdlibClient


logger = logging.getLogger(__name__)

_BOT_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT_SECONDS = 30
_REQUEST_TIMEOUT_SECONDS = _POLL_TIMEOUT_SECONDS + 5
_MAX_NETWORK_BACKOFF_SECONDS = 30


class ChatAccessClient(Protocol):
    def validate_chat(self, chat_id: int) -> bool: ...


class HttpResponse(Protocol):
    def read(self) -> bytes: ...


HttpOpener = Callable[..., HttpResponse]


@dataclass(frozen=True)
class BotStatus:
    enabled: bool
    reason: str | None = None


class CommandError(ValueError):
    """Raised when a command cannot be executed."""


class AdminCommands:
    """Pure command handlers for the management Bot."""

    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        registry: RouteRegistry,
        tdlib: ChatAccessClient,
        copy_service: CopyService,
    ) -> None:
        self._config = config
        self._store = store
        self._registry = registry
        self._tdlib = tdlib
        self._copy_service = copy_service
        self._pending_notifications: list[tuple[int, str]] = []

    def watch(
        self,
        actor_id: int,
        args: list[str],
    ) -> str:
        self._ensure_authorized(actor_id)
        if len(args) not in {1, 2}:
            raise CommandError(
                "Usage: /watch <source_id> [destination_id]"
            )

        source_id = _parse_chat_id(args[0], "source_id")
        if len(args) == 2:
            destination_id = _parse_chat_id(args[1], "destination_id")
        elif self._config.destination_id is None:
            raise CommandError(
                "DESTINATION is not configured; provide destination_id"
            )
        else:
            destination_id = self._config.destination_id

        if source_id == destination_id:
            raise CommandError("source_id and destination_id must differ")

        self._ensure_chat_access(source_id, "source")
        self._ensure_chat_access(destination_id, "destination")

        try:
            task = self._store.add_watch(
                source_id,
                destination_id,
                actor_id,
            )
        except DuplicateWatchError as error:
            raise CommandError(str(error)) from None

        self._registry.replace_dynamic(self._store.list_watches())
        return (
            f"Watch added: {task.source_id} -> {task.destination_id}"
        )

    def unwatch(self, actor_id: int, args: list[str]) -> str:
        self._ensure_authorized(actor_id)
        if len(args) != 1:
            raise CommandError("Usage: /unwatch <source_id>")

        source_id = _parse_chat_id(args[0], "source_id")
        if not self._store.remove_watch(source_id):
            raise CommandError(f"No watch found for source {source_id}")

        self._registry.replace_dynamic(self._store.list_watches())
        return f"Watch removed for source {source_id}"

    def list_watches(self, actor_id: int) -> str:
        self._ensure_authorized(actor_id)
        tasks = self._registry.dynamic_tasks
        if not tasks:
            return "No dynamic watch tasks configured."

        lines = ["Dynamic watch tasks:"]
        for task in tasks:
            lines.append(
                f"- {task.source_id} -> {task.destination_id}"
            )
        return "\n".join(lines)

    def copy(
        self,
        actor_id: int,
        chat_id: int,
        args: list[str],
    ) -> str:
        self._ensure_authorized(actor_id)
        if len(args) != 2:
            raise CommandError("Usage: /copy <source_id> <destination_id>")

        source_id = _parse_chat_id(args[0], "source_id")
        destination_id = _parse_chat_id(args[1], "destination_id")
        if source_id == destination_id:
            raise CommandError("source_id and destination_id must differ")

        self._ensure_chat_access(source_id, "source")
        self._ensure_chat_access(destination_id, "destination")

        route = Route(source_id, destination_id)

        def notify(result: HistoryJobResult) -> None:
            if result.request_chat_id != chat_id:
                return
            if result.status == "completed":
                text = (
                    f"Copy job {result.job_id} completed; "
                    f"{result.copied_count} messages copied."
                )
            elif result.status == "failed":
                text = (
                    f"Copy job {result.job_id} failed: "
                    f"{result.error_message or 'unknown error'}"
                )
            else:
                text = (
                    f"Copy job {result.job_id} ended with status "
                    f"{result.status}."
                )
            self._pending_notifications.append((chat_id, text))

        try:
            job = self._copy_service.start_history_job(
                route,
                actor_id,
                chat_id,
                notify,
            )
        except ActiveCopyJobError as error:
            raise CommandError(str(error)) from None
        except HistoryJobActiveError as error:
            raise CommandError(str(error)) from None
        except ServiceStoppedError as error:
            raise CommandError(str(error)) from None

        return (
            f"Copy job {job.id} queued for "
            f"{source_id} -> {destination_id}."
        )

    def _ensure_authorized(self, actor_id: int) -> None:
        if actor_id not in self._config.bot_admin_ids:
            raise CommandError("Unauthorized")

    def _ensure_chat_access(self, chat_id: int, label: str) -> None:
        if not self._tdlib.validate_chat(chat_id):
            raise CommandError(f"Cannot access {label} chat {chat_id}")


class AdminBot:
    """Optional long-polling management Bot."""

    def __init__(
        self,
        config: AppConfig,
        commands: AdminCommands,
        *,
        opener: HttpOpener | None = None,
        thread_factory: Callable[..., Thread] = Thread,
    ) -> None:
        self._config = config
        self._commands = commands
        self._opener = opener or urllib.request.urlopen
        self._thread_factory = thread_factory
        self._lock = RLock()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._offset = 0
        self._status = self._evaluate_status()

    @property
    def status(self) -> BotStatus:
        return self._status

    def start(self) -> BotStatus:
        with self._lock:
            if not self._status.enabled:
                return self._status
            if self._thread is not None:
                return self._status
            self._stop_event.clear()
            thread = self._thread_factory(
                target=self._poll_loop,
                name="telecopy-admin-bot",
                daemon=True,
            )
            self._thread = thread
            thread.start()
            return self._status

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout_seconds)
        with self._lock:
            self._thread = None

    def drain_notifications(self) -> list[tuple[int, str]]:
        pending = self._commands._pending_notifications
        notifications = list(pending)
        pending.clear()
        return notifications

    def send_message(self, chat_id: int, text: str) -> None:
        if not self._status.enabled or self._config.bot_token is None:
            return
        self._api_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
            },
        )

    def _evaluate_status(self) -> BotStatus:
        if not self._config.bot_token:
            return BotStatus(False, "BOT_TOKEN is not configured")
        if not self._config.bot_admin_ids:
            return BotStatus(
                False,
                "BOT_ADMIN_IDS is empty; management Bot disabled",
            )
        return BotStatus(True)

    def _poll_loop(self) -> None:
        backoff_seconds = 1
        while not self._stop_event.is_set():
            try:
                updates = self._fetch_updates()
            except urllib.error.HTTPError as error:
                if error.code == 401:
                    logger.error("Invalid BOT_TOKEN; management Bot disabled")
                    self._status = BotStatus(False, "Invalid BOT_TOKEN")
                    return
                logger.warning("Bot API HTTP error: %s", error)
                if self._wait(backoff_seconds):
                    backoff_seconds = min(
                        backoff_seconds * 2,
                        _MAX_NETWORK_BACKOFF_SECONDS,
                    )
                continue
            except urllib.error.URLError as error:
                logger.warning("Bot API network error: %s", error)
                if self._wait(backoff_seconds):
                    backoff_seconds = min(
                        backoff_seconds * 2,
                        _MAX_NETWORK_BACKOFF_SECONDS,
                    )
                continue
            except Exception:
                logger.exception("Bot polling failed unexpectedly")
                if self._wait(backoff_seconds):
                    backoff_seconds = min(
                        backoff_seconds * 2,
                        _MAX_NETWORK_BACKOFF_SECONDS,
                    )
                continue

            backoff_seconds = 1
            for update in updates:
                if self._stop_event.is_set():
                    return
                update_id = update.get("update_id")
                if type(update_id) is int:
                    self._offset = update_id + 1
                self._handle_update(update)

            for chat_id, text in self.drain_notifications():
                try:
                    self.send_message(chat_id, text)
                except Exception:
                    logger.exception(
                        "Failed to send copy-job notification to %d",
                        chat_id,
                    )

    def _fetch_updates(self) -> list[dict[str, Any]]:
        payload = self._api_request(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": _POLL_TIMEOUT_SECONDS,
                "allowed_updates": json.dumps(["message"]),
            },
        )
        if not payload.get("ok"):
            description = payload.get("description", "unknown error")
            raise urllib.error.URLError(description)
        result = payload.get("result")
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            return

        sender = message.get("from")
        if not isinstance(sender, dict):
            return
        actor_id = sender.get("id")
        if type(actor_id) is not int:
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return

        command, _, argument_text = text.partition(" ")
        command_name = command.split("@", 1)[0].casefold()
        args = argument_text.split() if argument_text else []
        chat_id = chat.get("id")
        if type(chat_id) is not int:
            return

        try:
            if command_name == "/watch":
                reply = self._commands.watch(actor_id, args)
            elif command_name == "/unwatch":
                reply = self._commands.unwatch(actor_id, args)
            elif command_name in {"/lswatch", "/listwatch"}:
                reply = self._commands.list_watches(actor_id)
            elif command_name == "/copy":
                reply = self._commands.copy(actor_id, chat_id, args)
            else:
                return
        except CommandError as error:
            reply = str(error)

        try:
            self.send_message(chat_id, reply)
        except Exception:
            logger.exception("Failed to send command reply to %d", chat_id)

    def _api_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        token = self._config.bot_token
        if token is None:
            raise RuntimeError("BOT_TOKEN is not configured")
        query = urllib.parse.urlencode(params)
        url = _BOT_API_BASE.format(token=token, method=method)
        if query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url)
        with self._opener(
            request,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise urllib.error.URLError("Bot API returned a non-object payload")
        return payload

    def _wait(self, seconds: float) -> bool:
        return self._stop_event.wait(seconds)


def _parse_chat_id(value: str, label: str) -> int:
    try:
        chat_id = int(value)
    except ValueError:
        raise CommandError(f"{label} must be an integer") from None
    if chat_id == 0:
        raise CommandError(f"{label} must not be zero")
    return chat_id


def build_admin_bot(
    config: AppConfig,
    store: StateStore,
    registry: RouteRegistry,
    tdlib: TdlibClient,
    copy_service: CopyService,
    *,
    opener: HttpOpener | None = None,
) -> AdminBot:
    commands = AdminCommands(config, store, registry, tdlib, copy_service)
    return AdminBot(config, commands, opener=opener)
