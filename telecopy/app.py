"""Daemon application lifecycle for TeleCopy."""

from __future__ import annotations

import hashlib
import logging
import shutil
import signal
import sys
from pathlib import Path
from threading import Event

from telecopy.admin_bot import AdminBot, build_admin_bot
from telecopy.config import AppConfig
from telecopy.copy_service import CopyService
from telecopy.database import StateStore
from telecopy.monitor import MonitorDispatcher
from telecopy.tasks import RouteRegistry
from telecopy.tdlib_client import TdlibClient


logger = logging.getLogger(__name__)

_LEGACY_COPY_MAP = Path("data/copy_map.json")
_SHUTDOWN_TIMEOUT_SECONDS = 10.0


class StartupError(RuntimeError):
    """Raised when the daemon cannot start."""


class TeleCopyApplication:
    """Connect, monitor routes, and optionally run the management Bot."""

    def __init__(
        self,
        config: AppConfig,
        *,
        store: StateStore | None = None,
        tdlib: TdlibClient | None = None,
        registry: RouteRegistry | None = None,
        copy_service: CopyService | None = None,
        monitor: MonitorDispatcher | None = None,
        admin_bot: AdminBot | None = None,
        shutdown_event: Event | None = None,
    ) -> None:
        self._config = config
        self._store = store or StateStore(
            config.data_directory / "telecopy.db"
        )
        self._tdlib = tdlib or TdlibClient(config)
        self._registry = registry or RouteRegistry(config.builtin_route)
        self._copy_service = copy_service or CopyService(
            self._tdlib,
            self._store,
            self._registry,
            send_copy=config.send_copy,
        )
        self._monitor = monitor or MonitorDispatcher(
            self._tdlib,
            self._copy_service,
            self._registry,
            config.builtin_route,
        )
        self._admin_bot = admin_bot
        self._shutdown_event = shutdown_event or Event()
        self._started = False
        self._previous_handlers: dict[int, object] = {}

    @classmethod
    def from_env(cls) -> "TeleCopyApplication":
        config = AppConfig.from_env()
        store = StateStore(config.data_directory / "telecopy.db")
        tdlib = TdlibClient(config)
        registry = RouteRegistry(config.builtin_route)
        copy_service = CopyService(
            tdlib,
            store,
            registry,
            send_copy=config.send_copy,
        )
        admin_bot = build_admin_bot(
            config,
            store,
            registry,
            tdlib,
            copy_service,
        )
        return cls(
            config,
            store=store,
            tdlib=tdlib,
            registry=registry,
            copy_service=copy_service,
            admin_bot=admin_bot,
        )

    def run(self) -> int:
        """Start the daemon and block until a shutdown signal arrives."""
        self._install_signal_handlers()
        try:
            self.start()
        except StartupError as error:
            logger.error("%s", error)
            return 1
        except Exception:
            logger.exception("TeleCopy failed to start")
            self.stop()
            return 1

        logger.info("TeleCopy daemon is running")
        try:
            while not self._shutdown_event.is_set():
                self._shutdown_event.wait(timeout=1.0)
        except KeyboardInterrupt:
            self._shutdown_event.set()
        finally:
            self.stop()
        return 0

    def start(self) -> None:
        if self._started:
            return

        configure_logging()
        self._store.initialize()
        fingerprint = _credential_fingerprint(self._config)
        credentials_changed = self._store.reset_for_credentials(fingerprint)
        if credentials_changed:
            logger.info("Credentials changed; clearing session and task state")
            clear_session_files(self._config)

        interrupted_jobs = self._store.interrupt_active_jobs()
        if interrupted_jobs:
            logger.info("Marked %d stale copy jobs as interrupted", interrupted_jobs)

        migrated = self._store.migrate_legacy_copy_map(
            _LEGACY_COPY_MAP,
            self._config.builtin_route,
        )
        if migrated:
            logger.info("Migrated %d legacy copy-map entries", migrated)

        try:
            self._tdlib.connect()
        except Exception as error:
            if not sys.stdin.isatty():
                raise StartupError(
                    "Telegram authorization is required but no interactive "
                    "terminal is available. Run "
                    "`docker compose run --rm telecopy` once to log in."
                ) from error
            raise

        self._registry.replace_dynamic(self._store.list_watches())
        self._copy_service.start()
        self._monitor.start()

        if self._admin_bot is not None:
            status = self._admin_bot.start()
            if status.enabled:
                logger.info("Management Bot started")
            elif status.reason:
                logger.warning("Management Bot disabled: %s", status.reason)

        self._started = True

    def stop(self) -> None:
        if not self._started and self._admin_bot is None:
            self._restore_signal_handlers()
            return

        if self._admin_bot is not None:
            self._admin_bot.stop()

        try:
            self._monitor.stop()
        except Exception:
            logger.exception("Failed to stop monitor dispatcher")

        if not self._copy_service.stop(_SHUTDOWN_TIMEOUT_SECONDS):
            logger.warning("Copy service did not stop within the deadline")

        try:
            self._tdlib.stop()
        except Exception:
            logger.exception("Failed to stop TDLib client")

        self._started = False
        self._restore_signal_handlers()

    def _install_signal_handlers(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (AttributeError, ValueError, OSError):
                continue

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (AttributeError, ValueError, OSError):
                continue
        self._previous_handlers.clear()

    def _handle_signal(self, signum, _frame) -> None:
        logger.info("Received signal %s; shutting down", signum)
        self._shutdown_event.set()


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )


def _credential_fingerprint(config: AppConfig) -> str:
    payload = (
        f"{config.api_id}\x00"
        f"{config.api_hash}\x00"
        f"{config.phone}\x00"
        f"{config.db_password}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def clear_session_files(config: AppConfig) -> None:
    """Remove TDLib session data without deleting the SQLite database."""
    data_dir = config.data_directory
    database_path = data_dir / "telecopy.db"

    files_directory = Path(config.files_directory)
    if not files_directory.is_absolute():
        files_directory = Path.cwd() / files_directory
    _clear_directory_contents(files_directory)

    legacy_session = Path("tdlib-session")
    _clear_directory_contents(legacy_session)

    if data_dir.exists():
        for entry in data_dir.iterdir():
            if entry.resolve() == database_path.resolve():
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)


def _clear_directory_contents(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        path.unlink(missing_ok=True)
        return
    for entry in path.iterdir():
        target = entry
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
