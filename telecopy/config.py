"""Typed application configuration loaded from environment variables."""

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path

from dotenv import load_dotenv


logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when application configuration is invalid."""


@dataclass(frozen=True)
class Route:
    source_id: int
    destination_id: int


@dataclass(frozen=True)
class AppConfig:
    phone: str = field(repr=False)
    api_id: str
    api_hash: str = field(repr=False)
    db_password: str = field(repr=False)
    source_id: int | None
    destination_id: int | None
    bot_token: str | None = field(repr=False)
    bot_admin_ids: frozenset[int]
    files_directory: str
    send_copy: bool
    proxy_url: str | None = field(repr=False)
    data_directory: Path

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()

        required_names = ("PHONE", "API_ID", "API_HASH", "DB_PASSWORD")
        required_values = {
            name: (os.getenv(name) or "").strip() for name in required_names
        }
        missing_names = [
            name for name in required_names if not required_values[name]
        ]
        if missing_names:
            raise ConfigError(
                "Missing required configuration: " + ", ".join(missing_names)
            )

        source_id = _parse_optional_integer("SOURCE")
        destination_id = _parse_optional_integer("DESTINATION")
        if source_id is not None and destination_id is None:
            logger.warning(
                "SOURCE is set but DESTINATION is missing; "
                "built-in route disabled"
            )

        return cls(
            phone=required_values["PHONE"],
            api_id=required_values["API_ID"],
            api_hash=required_values["API_HASH"],
            db_password=required_values["DB_PASSWORD"],
            source_id=source_id,
            destination_id=destination_id,
            bot_token=_optional_text("BOT_TOKEN"),
            bot_admin_ids=_parse_admin_ids(),
            files_directory=_optional_text("FILES_DIRECTORY")
            or "data/tdlib_files",
            send_copy=_parse_boolean("SEND_COPY", default=True),
            proxy_url=_optional_text("PROXY_URL"),
            data_directory=Path("data"),
        )

    @property
    def builtin_route(self) -> Route | None:
        if self.source_id is None or self.destination_id is None:
            return None
        return Route(self.source_id, self.destination_id)


def _optional_text(name: str) -> str | None:
    value = (os.getenv(name) or "").strip()
    return value or None


def _parse_optional_integer(name: str) -> int | None:
    value = _optional_text(name)
    if value is None:
        return None

    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


def _parse_admin_ids() -> frozenset[int]:
    value = _optional_text("BOT_ADMIN_IDS")
    if value is None:
        return frozenset()

    parts = value.split(",")
    if any(not part.strip() for part in parts):
        raise ConfigError(
            "BOT_ADMIN_IDS must be a comma-separated list of integers"
        )

    try:
        return frozenset(int(part.strip()) for part in parts)
    except ValueError:
        raise ConfigError(
            "BOT_ADMIN_IDS must be a comma-separated list of integers"
        ) from None


def _parse_boolean(name: str, default: bool) -> bool:
    value = _optional_text(name)
    if value is None:
        return default

    normalized = value.casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ConfigError(
        f"{name} must be one of: true, false, 1, 0, yes, no, on, off"
    )
