# TeleCopy Daemon Watch Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the interactive menu with an automatically connected Docker daemon that persists multi-route watches in SQLite and exposes authorized Bot commands for route management and history copying.

**Architecture:** Keep TDLib as the user-session transport and use a small standard-library HTTP Bot API client for management commands, avoiding the conflicting `telegram` namespaces of `python-telegram` and `python-telegram-bot`. Split configuration, persistence, routing, forwarding, Bot, and lifecycle into focused modules; serialize forwarding through one priority scheduler so real-time messages preempt historical work.

**Tech Stack:** Python 3.11, python-telegram/TDLib, Telegram Bot HTTP API, SQLite, pytest, Docker Compose.

---

## File Structure

Create:

- `telecopy/__init__.py`: package marker.
- `telecopy/config.py`: environment parsing and validation.
- `telecopy/database.py`: schema, transactions, credential reset, legacy migration.
- `telecopy/tasks.py`: route models and immutable effective-route registry.
- `telecopy/tdlib_client.py`: login, proxy parsing, chat access, history iteration.
- `telecopy/copy_service.py`: forwarding scheduler, deduplication, history jobs.
- `telecopy/monitor.py`: lightweight TDLib update dispatcher.
- `telecopy/admin_bot.py`: authorization and Bot command handlers.
- `telecopy/app.py`: process lifecycle, signals, startup, and shutdown.
- `tests/`: unit and integration-style tests using fake adapters.

Modify:

- `main.py`: minimal daemon entry point.
- `requirements.txt`: pin runtime and test-compatible dependencies.
- `.env.example`: add Bot settings and document optional routes.
- `Dockerfile`: copy package/tests-independent runtime files.
- `docker-compose.yml`: add restart policy while retaining interactive login support.
- `README.md`: replace menu instructions with daemon and Bot usage.

Delete no runtime data automatically outside the credential-reset transaction.

---

### Task 1: Package Scaffold and Typed Configuration

**Files:**
- Create: `telecopy/__init__.py`
- Create: `telecopy/config.py`
- Create: `tests/test_config.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add failing configuration tests**

Cover required credentials, optional SOURCE/DESTINATION combinations, parsed
admin IDs, booleans, and `PROXY_URL`:

```python
def test_load_config_allows_empty_builtin_route(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.delenv("SOURCE", raising=False)
    monkeypatch.delenv("DESTINATION", raising=False)

    config = AppConfig.from_env()

    assert config.builtin_route is None


def test_source_without_destination_has_no_builtin_route(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("SOURCE", "-1001")
    monkeypatch.delenv("DESTINATION", raising=False)

    config = AppConfig.from_env()

    assert config.builtin_route is None


def test_bot_admin_ids_parse_comma_separated_integers(monkeypatch):
    set_required_env(monkeypatch)
    monkeypatch.setenv("BOT_ADMIN_IDS", "123, 456")

    config = AppConfig.from_env()

    assert config.bot_admin_ids == frozenset({123, 456})
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/test_config.py -v`

Expected: FAIL because `telecopy.config` does not exist.

- [ ] **Step 3: Implement `AppConfig`**

Use frozen dataclasses:

```python
@dataclass(frozen=True)
class Route:
    source_id: int
    destination_id: int


@dataclass(frozen=True)
class AppConfig:
    phone: str
    api_id: str
    api_hash: str
    db_password: str
    source_id: int | None
    destination_id: int | None
    bot_token: str | None
    bot_admin_ids: frozenset[int]
    files_directory: str
    send_copy: bool
    proxy_url: str | None
    data_directory: Path

    @property
    def builtin_route(self) -> Route | None:
        if self.source_id is None or self.destination_id is None:
            return None
        return Route(self.source_id, self.destination_id)
```

`from_env()` must reject missing PHONE/API_ID/API_HASH/DB_PASSWORD with a
single `ConfigError` listing field names. Do not reject absent Bot or route
settings.

- [ ] **Step 4: Pin dependencies**

Install current compatible releases through pip, then record exact resolved
versions for `python-dotenv`, `python-telegram`, `setuptools`, `tqdm`, and
`pytest`. Do not install `python-telegram-bot`, because it conflicts with the
TDLib binding's top-level `telegram` package. Do not invent versions.

Run: `python -m pip install -r requirements.txt pytest`

- [ ] **Step 5: Run configuration tests**

Run: `python -m pytest tests/test_config.py -v`

Expected: PASS.

---

### Task 2: SQLite State Store and Credential Reset

**Files:**
- Create: `telecopy/database.py`
- Create: `tests/test_database.py`

- [ ] **Step 1: Add failing database tests**

Tests must cover schema creation, unique dynamic source, route-level copy
records, stale job interruption, credential reset, and persistence:

```python
def test_watch_source_is_unique(store):
    store.add_watch(-1001, -2001, created_by=123)

    with pytest.raises(DuplicateWatchError):
        store.add_watch(-1001, -2002, created_by=123)


def test_credential_change_clears_business_state(store):
    store.set_credential_fingerprint("old")
    store.add_watch(-1001, -2001, created_by=123)
    store.record_copy(-1001, -2001, 10, 20)

    changed = store.reset_for_credentials("new")

    assert changed is True
    assert store.list_watches() == []
    assert not store.was_copied(-1001, -2001, 10)
```

- [ ] **Step 2: Run database tests and verify RED**

Run: `python -m pytest tests/test_database.py -v`

Expected: FAIL because `StateStore` is absent.

- [ ] **Step 3: Implement schema and transactional methods**

Implement:

```python
class StateStore:
    def initialize(self) -> None: ...
    def reset_for_credentials(self, fingerprint: str) -> bool: ...
    def add_watch(self, source_id: int, destination_id: int, created_by: int) -> WatchTask: ...
    def remove_watch(self, source_id: int) -> bool: ...
    def list_watches(self) -> list[WatchTask]: ...
    def was_copied(self, source_id: int, destination_id: int, message_id: int) -> bool: ...
    def record_copy(self, source_id: int, destination_id: int, message_id: int, destination_message_id: int) -> None: ...
    def create_copy_job(self, source_id: int, destination_id: int, requested_by: int, request_chat_id: int) -> CopyJob: ...
    def update_copy_job(self, job_id: int, status: str, copied_count: int, error_message: str | None = None) -> None: ...
    def interrupt_active_jobs(self) -> int: ...
```

Use parameterized SQL, WAL mode, explicit transactions, UTC ISO timestamps,
and a fresh connection per thread through a connection factory.

- [ ] **Step 4: Implement legacy `copy_map.json` migration**

Add:

```python
def migrate_legacy_copy_map(
    self,
    path: Path,
    route: Route | None,
) -> int:
    ...
```

When a route exists, import entries transactionally, rename the file to
`copy_map.json.migrated`, and set a metadata marker. Without a route, leave the
file unchanged.

- [ ] **Step 5: Run database tests**

Run: `python -m pytest tests/test_database.py -v`

Expected: PASS.

---

### Task 3: Effective Route Registry

**Files:**
- Create: `telecopy/tasks.py`
- Create: `tests/test_tasks.py`

- [ ] **Step 1: Add failing registry tests**

```python
def test_registry_deduplicates_same_builtin_and_dynamic_route():
    registry = RouteRegistry(Route(-1001, -2001))
    registry.replace_dynamic([WatchTask(-1001, -2001, 123, "now")])

    assert registry.destinations_for(-1001) == (-2001,)


def test_registry_keeps_distinct_destinations_for_same_source():
    registry = RouteRegistry(Route(-1001, -2001))
    registry.replace_dynamic([WatchTask(-1001, -2002, 123, "now")])

    assert set(registry.destinations_for(-1001)) == {-2001, -2002}
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/test_tasks.py -v`

- [ ] **Step 3: Implement immutable snapshots**

`RouteRegistry` must use an `RLock`, publish mappings whose values are sorted
tuples, expose `destinations_for(source_id)`, `contains(route)`, and refresh
dynamic rows only after database transactions succeed.

- [ ] **Step 4: Run route tests**

Run: `python -m pytest tests/test_tasks.py -v`

Expected: PASS.

---

### Task 4: TDLib Adapter

**Files:**
- Create: `telecopy/tdlib_client.py`
- Create: `tests/test_tdlib_client.py`

The existing `main.py` remains unchanged until Task 8 replaces the menu entry
point, preventing a partially migrated runtime.

- [ ] **Step 1: Add failing adapter tests**

Cover SOCKS5 URL parsing, constructor arguments, chat access validation,
handler registration/removal signatures, and history iteration.

```python
def test_parse_authenticated_socks5_url():
    proxy = parse_proxy_url("socks5://user:p%40ss@host:1080")

    assert proxy.server == "host"
    assert proxy.port == 1080
    assert proxy.type["username"] == "user"
    assert proxy.type["password"] == "p@ss"
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/test_tdlib_client.py -v`

- [ ] **Step 3: Extract `TdlibClient`**

Expose only:

```python
class TdlibClient:
    def connect(self) -> None: ...
    def stop(self) -> None: ...
    def add_new_message_handler(self, handler: Callable[[dict], None]) -> None: ...
    def remove_new_message_handler(self, handler: Callable[[dict], None]) -> None: ...
    def validate_chat(self, chat_id: int) -> bool: ...
    def iter_history(self, chat_id: int) -> Iterator[dict]: ...
    def forward_message(self, source_id: int, destination_id: int, message_id: int, send_copy: bool) -> int: ...
```

Keep python-telegram-specific method names inside this adapter.

- [ ] **Step 4: Run adapter tests**

Run: `python -m pytest tests/test_tdlib_client.py -v`

Expected: PASS.

---

### Task 5: Priority Forwarding and History Jobs

**Files:**
- Create: `telecopy/copy_service.py`
- Create: `tests/test_copy_service.py`

- [ ] **Step 1: Add failing scheduler tests**

Test real-time priority, deduplication, route revalidation, one active history
job, completion/failure status, and interruption behavior.

```python
def test_realtime_item_runs_before_queued_history_item(service, fake_client):
    service.enqueue_history(route, message_id=1, job_id=7)
    service.enqueue_realtime(route, message_id=2, dynamic=True)

    service.process_next()

    assert fake_client.forwarded[0].message_id == 2


def test_removed_dynamic_route_is_skipped(service, registry, fake_client):
    service.enqueue_realtime(route, message_id=2, dynamic=True)
    registry.replace_dynamic([])

    service.process_next()

    assert fake_client.forwarded == []
```

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/test_copy_service.py -v`

- [ ] **Step 3: Implement scheduler**

Use one worker thread and `PriorityQueue` with stable sequence numbers:

```python
REALTIME_PRIORITY = 0
HISTORY_PRIORITY = 10

@dataclass(order=True)
class QueueItem:
    priority: int
    sequence: int
    route: Route = field(compare=False)
    message_id: int = field(compare=False)
    dynamic_route: bool = field(compare=False)
    job_id: int | None = field(compare=False)
```

All forwarding, retry, FloodWait, and copy-record writes occur in this worker.
The update handler never sleeps or calls TDLib forwarding APIs.

- [ ] **Step 4: Implement single history-job producer**

Persist the job, reject duplicate active routes, enumerate history, enqueue
low-priority items, update copied counts, and notify through a callback.
Cancellation on shutdown marks the job `interrupted`.

- [ ] **Step 5: Run scheduler tests**

Run: `python -m pytest tests/test_copy_service.py -v`

Expected: PASS.

---

### Task 6: Monitor Dispatcher

**Files:**
- Create: `telecopy/monitor.py`
- Create: `tests/test_monitor.py`

- [ ] **Step 1: Add failing dispatcher tests**

Cover excluded messages, unmatched sources, one source to multiple destinations,
and identical-route deduplication.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/test_monitor.py -v`

- [ ] **Step 3: Implement dispatcher**

```python
class MonitorDispatcher:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def handle_update(self, update: dict) -> None: ...
```

`handle_update` obtains the route snapshot and enqueues real-time items only.

- [ ] **Step 4: Run dispatcher tests**

Run: `python -m pytest tests/test_monitor.py -v`

Expected: PASS.

---

### Task 7: Authorized Management Bot

**Files:**
- Create: `telecopy/admin_bot.py`
- Create: `tests/test_admin_bot.py`

- [ ] **Step 1: Add failing Bot command tests**

Use fake HTTP responses and Bot API update dictionaries and cover:

- private chat requirement;
- admin allowlist;
- `/watch` default destination and missing-default error;
- duplicate source rejection;
- `/unwatch` immediate refresh;
- `/lswatch` excluding built-in routes;
- `/copy` required arguments and queued response.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/test_admin_bot.py -v`

- [ ] **Step 3: Implement command service**

Separate command logic from Telegram objects:

```python
class AdminCommands:
    async def watch(self, actor_id: int, args: list[str]) -> str: ...
    async def unwatch(self, actor_id: int, args: list[str]) -> str: ...
    async def list_watches(self, actor_id: int) -> str: ...
    async def copy(self, actor_id: int, chat_id: int, args: list[str]) -> str: ...
```

The long-poll loop parses Bot API update dictionaries, performs
authorization/private-chat checks, calls these methods, and sends returned
text through `sendMessage`. Never include secrets in requests logs or
responses.

- [ ] **Step 4: Implement optional Bot lifecycle**

Expose `start()`, `stop()`, and `send_job_result(chat_id, message)` around a
dedicated polling thread. Use `urllib.request` with explicit connect/read
timeouts, monotonically advance the `getUpdates` offset, retry transient
network failures with bounded backoff, and wake promptly on shutdown. Invalid
token startup logs an error and returns disabled status without stopping
monitoring.

- [ ] **Step 5: Run Bot tests**

Run: `python -m pytest tests/test_admin_bot.py -v`

Expected: PASS.

---

### Task 8: Daemon Application Lifecycle

**Files:**
- Create: `telecopy/app.py`
- Create: `tests/test_app.py`
- Replace: `main.py`

- [ ] **Step 1: Add failing lifecycle tests**

Verify startup ordering, optional Bot, automatic monitor start, credential
reset before route load, and reverse-order shutdown.

- [ ] **Step 2: Run and verify RED**

Run: `python -m pytest tests/test_app.py -v`

- [ ] **Step 3: Implement `TeleCopyApplication`**

```python
class TeleCopyApplication:
    async def run(self) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

Install SIGINT/SIGTERM handlers, connect before accepting Bot commands, and
fail with a non-zero exit when user authorization cannot complete.

- [ ] **Step 4: Replace menu entry point**

`main.py` becomes:

```python
import asyncio

from telecopy.app import TeleCopyApplication


if __name__ == "__main__":
    raise SystemExit(asyncio.run(TeleCopyApplication.from_env().run()))
```

- [ ] **Step 5: Run lifecycle and full tests**

Run: `python -m pytest -v`

Expected: all tests PASS.

---

### Task 9: Docker and Documentation

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `.dockerignore`

- [ ] **Step 1: Update image contents**

Copy `telecopy/` and `main.py`; keep `/app/data` as the only volume. Ensure
tests/docs are excluded from the release context where safe.

- [ ] **Step 2: Update Compose**

Add:

```yaml
restart: unless-stopped
```

Keep `stdin_open: true` and `tty: true` for first-login compatibility.

- [ ] **Step 3: Update environment example**

Document optional SOURCE/DESTINATION and add:

```env
BOT_TOKEN=
BOT_ADMIN_IDS=
```

- [ ] **Step 4: Rewrite README operation flow**

Document:

- one-time interactive login;
- detached daemon startup;
- `/watch`, `/unwatch`, `/lswatch`, `/copy`;
- default destination behavior;
- admin allowlist;
- credential reset semantics;
- job interruption on restart;
- GHCR update procedure.

- [ ] **Step 5: Run documentation/config checks**

Run: `git diff --check`

Expected: no output and exit code 0.

---

### Task 10: Final Verification

**Files:**
- Review all modified files.

- [ ] **Step 1: Run complete automated checks**

Run:

```bash
python -m pytest -v
python -m py_compile main.py telecopy/*.py
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 2: Build the Docker image**

Run:

```bash
docker build --platform linux/amd64 -t telecopy:test .
```

Expected: image builds successfully.

- [ ] **Step 3: Smoke-test startup without Bot**

Use a temporary data directory containing an authorized TDLib session and
verify automatic connect plus monitor registration.

- [ ] **Step 4: Smoke-test Bot commands**

With test credentials, verify allowlist rejection, watch CRUD, list output,
history-job queueing, completion notification, and persistence across ordinary
container restart.

- [ ] **Step 5: Review the final diff against the design**

Confirm every requirement in
`docs/superpowers/specs/2026-07-12-daemon-watch-bot-design.md` maps to code and
tests, and no interactive menu remains.
