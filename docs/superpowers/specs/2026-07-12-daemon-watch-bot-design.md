# TeleCopy Daemon Watch Bot Design

## Goal

Convert TeleCopy from an interactive single-route CLI into a Docker-friendly
daemon that automatically connects to the persisted Telegram user session,
monitors built-in and database-backed routes, and exposes an authorized
Telegram Bot interface for managing routes and starting history-copy jobs.

## Confirmed Requirements

- Startup automatically connects the TDLib user client and starts monitoring.
- First login remains interactive through
  `docker compose run --rm telecopy`; later starts use
  `docker compose up -d`.
- Remove the startup menu and automatically run the service lifecycle.
- `SOURCE` and `DESTINATION` are optional:
  - no `SOURCE` means no built-in route;
  - a built-in route exists only when both values are present;
  - `SOURCE` without `DESTINATION` logs a warning and creates no built-in route.
- `BOT_TOKEN` is optional. Its absence disables only the management Bot;
  built-in and persisted dynamic routes continue running.
- `BOT_ADMIN_IDS` is a comma-separated allowlist of Telegram numeric user IDs.
- Bot commands are accepted only in private chats from allowlisted users.
- Dynamic watch tasks are stored in SQLite and keyed uniquely by `source_id`.
- Built-in routes are not stored in SQLite and are omitted from `/lswatch`.
- Built-in and dynamic routes may share a source. Effective routes are
  deduplicated by `(source_id, destination_id)`.
- Credential changes clear the TDLib session, dynamic watch tasks, copy
  progress, and copy-job history.
- Existing `copy_map.json` data is migrated to the built-in
  `SOURCE -> DESTINATION` route when both values are configured.
- Historical copy jobs run one at a time in the background. They are not
  resumed after process restart.
- Real-time forwarding has priority over historical copying.

## Architecture

Use one process with explicit internal boundaries:

```text
main.py
  -> application lifecycle
       -> configuration
       -> SQLite state store
       -> TDLib user client
       -> route registry
       -> monitor dispatcher
       -> forwarding scheduler
       -> optional management Bot
```

The TDLib user client remains responsible for reading chats and forwarding
messages. A small standard-library HTTP Bot API client performs long polling
for management commands and status replies. This avoids the incompatible
top-level `telegram` package namespaces used by `python-telegram` and
`python-telegram-bot`. The two clients do not share authentication or transport
responsibilities.

Recommended module layout:

```text
telecopy/
  __init__.py
  app.py
  config.py
  database.py
  tdlib_client.py
  tasks.py
  monitor.py
  copy_service.py
  admin_bot.py
main.py
```

`main.py` is a minimal entry point. Existing forwarding behavior moves behind
the service boundaries without unrelated feature changes.

## Configuration

Required user-session settings:

```env
PHONE=
API_ID=
API_HASH=
DB_PASSWORD=
```

Optional runtime settings:

```env
SOURCE=
DESTINATION=
BOT_TOKEN=
BOT_ADMIN_IDS=
FILES_DIRECTORY=data/tdlib_files
SEND_COPY=true
PROXY_URL=
```

If `BOT_TOKEN` is configured without valid `BOT_ADMIN_IDS`, the management Bot
is disabled and an error is logged. Monitoring remains active.

## Startup and Shutdown

Startup order:

1. Load and validate configuration.
2. Open `/app/data/telecopy.db` and apply schema migrations.
3. Compare the stored credential fingerprint with the current credentials.
4. On credential change, clear session files and all business-state rows while
   preserving the bind-mounted `/app/data` directory and database schema.
5. Mark stale `queued` or `running` history jobs as `interrupted`.
6. Migrate a legacy `copy_map.json` to the built-in route when possible.
7. Connect the TDLib user client. Interactive code/password prompts are allowed
   when stdin is attached.
8. Load built-in and dynamic routes.
9. Register one `updateNewMessage` handler.
10. Start the forwarding scheduler.
11. Start Bot polling when configured.
12. Wait for process termination.

If authorization is required but no interactive stdin is available, startup
fails with a clear error and non-zero exit code instead of hanging.

Shutdown order:

1. Stop accepting Bot commands.
2. Remove the TDLib update handler.
3. Stop accepting new forwarding work.
4. Allow the in-flight forwarding operation to finish up to a bounded timeout.
5. Stop Bot polling and TDLib.
6. Close database connections.

SIGINT and SIGTERM use the same shutdown path.

## Route Model

The route registry has two sources:

- Built-in route from environment variables.
- Dynamic routes from `watch_tasks`.

The effective route snapshot is built as a set of
`(source_id, destination_id)` pairs. This provides these semantics:

- identical built-in and dynamic routes forward once;
- identical sources with different destinations forward once per destination;
- `/lswatch` returns only dynamic rows;
- deleting a dynamic route does not affect a matching built-in route.

Registry updates are protected by a lock and published as immutable snapshots
so the TDLib handler can read routes while Bot commands modify them.

## SQLite Schema

Database path: `/app/data/telecopy.db`.

```sql
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE watch_tasks (
    source_id INTEGER PRIMARY KEY,
    destination_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE copy_records (
    source_id INTEGER NOT NULL,
    destination_id INTEGER NOT NULL,
    source_message_id INTEGER NOT NULL,
    destination_message_id INTEGER NOT NULL,
    copied_at TEXT NOT NULL,
    PRIMARY KEY (source_id, destination_id, source_message_id)
);

CREATE TABLE copy_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    destination_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    requested_by INTEGER NOT NULL,
    request_chat_id INTEGER NOT NULL,
    copied_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
```

SQLite uses WAL mode, transactions, foreign-thread-safe connection handling,
and parameterized queries. `copy_jobs` is an audit/status table, not a recovery
queue. At startup, stale active jobs become `interrupted`.

## Bot Commands

All commands require a private chat and an allowlisted `effective_user.id`.
Unauthorized callers receive a generic rejection.

### `/watch <source_id> [destination_id]`

- Validate IDs as signed 64-bit integers.
- If destination is omitted, use `DESTINATION`.
- If neither is available, return a usage/configuration error.
- Validate that the TDLib user session can access both chats.
- Insert a new row transactionally.
- Reject an existing dynamic `source_id`; do not overwrite it.
- Refresh the route snapshot before returning success.

### `/unwatch <source_id>`

- Delete only the dynamic task for the source.
- Return not-found when no dynamic task exists.
- Refresh the route snapshot immediately.
- A currently executing forward cannot be revoked.
- Queued work revalidates the effective route before execution and is skipped
  when the route no longer exists.

### `/lswatch`

- List dynamic tasks ordered by `source_id`.
- Do not display the built-in route.
- Return an explicit empty-state message.

### `/copy <source_id> <destination_id>`

- Both IDs are required.
- The route need not exist as a watch task.
- Reject a duplicate active job for the same route.
- Persist a queued job and return its job ID immediately.
- Notify the originating Bot chat on completion or failure.

## Monitoring and Forwarding

Register exactly one TDLib `updateNewMessage` handler. The handler:

1. extracts `source_id`;
2. reads an immutable route snapshot;
3. filters excluded service messages;
4. enqueues one high-priority item per unique destination;
5. returns without performing network calls or sleeping.

A single forwarding scheduler serializes `forwardMessages` calls. It always
selects real-time items before historical items. Serialization prevents
duplicate races, TDLib concurrency uncertainty, and excessive FloodWait.

Before forwarding, the worker:

- rechecks that dynamic watch work still has an effective route;
- checks `copy_records` for the route and source message;
- forwards the message;
- writes the success record transactionally.

History-copy jobs reuse the same route-level deduplication and forwarding
implementation. Only one history job is active. A restart does not resume it,
but a newly submitted job skips messages already present in `copy_records`.

## Credential Changes and Legacy Migration

The credential fingerprint covers `API_ID`, `API_HASH`, `PHONE`, and
`DB_PASSWORD`. A change is evaluated before workers and Bot polling start.

On change:

- stop/remove old TDLib session files;
- delete rows from `watch_tasks`, `copy_records`, and `copy_jobs`;
- store the new fingerprint;
- retain the SQLite file and schema.

Legacy `data/copy_map.json` migration occurs once when:

- both `SOURCE` and `DESTINATION` are configured;
- the migration marker is absent.

Entries become `copy_records` for that route. The migration is transactional;
the old file is renamed after success and the migration marker is recorded.
If no complete built-in route exists, the old file is left untouched and a
warning is logged.

## Error Handling

- Invalid environment values fail startup with field-specific messages.
- Invalid Bot arguments return command usage without raising into polling.
- A bad `BOT_TOKEN` disables management while monitoring remains active.
- Database writes use transactions and report failures without publishing an
  in-memory route change.
- Forwarding retains bounded retries and FloodWait handling.
- A failed forward does not remove its watch task.
- Bot notification failures are logged and do not change job results.
- No token, password, API hash, or proxy credentials are written to logs.

## Docker Behavior

The image starts the daemon directly. The Compose service uses:

```yaml
restart: unless-stopped
volumes:
  - ./data:/app/data
```

`stdin_open` and `tty` remain available for the one-time interactive login.
Normal operation uses `docker compose up -d`.

Application logs go to stdout/stderr. The container does not require a
persistent `telecopy.log`.

## Dependencies

Do not install `python-telegram-bot`, because it conflicts with the
`python-telegram` TDLib binding at the top-level `telegram` package. The
management Bot uses the Python standard-library HTTP client. Pin all runtime
dependencies used by the release image to avoid rebuilding the same source
against incompatible APIs.

## Testing

Unit tests cover:

- optional SOURCE/DESTINATION combinations;
- Bot allowlist and private-chat authorization;
- command parsing and default destination behavior;
- unique dynamic source enforcement;
- route merging and route-pair deduplication;
- immediate route removal/revalidation;
- credential reset behavior;
- SQLite transactions and legacy migration;
- proxy parsing;
- stale job interruption;
- history-job duplicate rejection;
- real-time priority and route-level copy deduplication.

Integration tests use fake TDLib and Bot adapters to cover:

- automatic connect and handler registration;
- startup loading of built-in and dynamic routes;
- one source routing to multiple destinations;
- real-time traffic preempting history work;
- completion/failure Bot notifications;
- optional/invalid Bot configuration;
- graceful SIGTERM shutdown.

Docker smoke tests cover:

1. interactive first login;
2. detached automatic reconnect;
3. persistence of dynamic tasks across ordinary restart;
4. clearing dynamic tasks after credential change;
5. non-resumption of interrupted history jobs.

## Out of Scope

- Synchronizing edited or deleted messages.
- Preserving Telegram media albums as grouped forwards.
- Webhook-based Bot deployment.
- Multiple process replicas sharing one SQLite database.
- Restoring interrupted history jobs automatically.
- Supporting more than one dynamic destination per source.
