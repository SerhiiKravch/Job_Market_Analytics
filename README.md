# Telegram Job Analytics

Local analytics for observed job advertising in one Telegram channel, covering IT
and digital-business roles. The MVP is being implemented incrementally.

Currently implemented: an installable Python package, CLI help/version, local
settings validation, project-root resolution, and privacy-preserving logging.
Database migrations, parsing, processing, Telegram login/collection, review,
reports, scheduling, and evaluation are not implemented yet. Accuracy is unmeasured.

## Installation and checks

The supported interpreter is Python **3.12** (`>=3.12,<3.13`). Install Python 3.12
and `uv`, then run from the repository root:

```sh
uv sync --locked --python 3.12
uv run --locked python -m job_market --help
uv run --locked job-market --version
uv run --locked python -m job_market config check
uv run --locked --offline pytest
uv run --locked --offline ruff check .
uv run --locked --offline ruff format --check .
```

`uv.lock` records exact dependency versions and artifact hashes. Initial
installation needs access to the package index; tests and configuration checks
operate offline once dependencies are installed. Neither installation nor tests
authenticate or connect to Telegram. No service starts automatically.

## Configuration

Defaults are enough for offline checks. To customize them:

```sh
cp .env.example .env
uv run --locked python -m job_market config check
```

Real environment variables override values in `.env`. Telegram placeholders are
accepted by offline checks. The separate command below checks the shape of the
configured API ID, API hash, and channel reference locally; it never connects,
authenticates, joins a channel, or verifies access:

```sh
uv run --locked python -m job_market config check --telegram
```

API ID/hash come from your own Telegram application. Leave phone numbers, login
codes, and two-step passwords out of `.env`; future interactive login will prompt
for them. Keep `.env` and session files private.

The default project root is the working directory. `.env`, relative session and
report paths, and relative SQLite database paths are resolved against that root.
Loading settings does not create directories, databases, or sessions. From
another directory, use the installed interpreter and an explicit root:

```sh
/absolute/project/path/.venv/bin/python -m job_market \
  --project-root /absolute/project/path config check
```

Put the global `--project-root` option before the command. Use absolute paths
when runtime files should live elsewhere.

| Setting | Default / meaning |
|---|---|
| `TELEGRAM_API_ID` | Optional offline; positive numeric API ID for future Telegram commands |
| `TELEGRAM_API_HASH` | Optional offline; 32 hexadecimal characters for Telegram commands |
| `TELEGRAM_CHANNEL` | Optional offline; already accessible channel reference, resolved during future collection |
| `TELEGRAM_SESSION_PATH` | `./var/telegram.session` |
| `DATABASE_URL` | `sqlite:///./var/jobs.db` |
| `BACKFILL_DAYS` | `30`; integer from 1 to 30, for eventual first-import initialization |
| `REPORT_OUTPUT_DIR` | `./var/reports` |
| `REPORT_TIMEZONE` | `Europe/Warsaw`; valid IANA timezone, for report calendar boundaries |
| `LOG_LEVEL` | `INFO`; also DEBUG, WARNING, ERROR, CRITICAL (case insensitive) |

SQLite file URLs and `sqlite:///:memory:` are supported configuration forms.
SQLite URI query parameters and async drivers are rejected. PostgreSQL URLs can
be validated without connecting; PostgreSQL drivers and integration validation
are deferred. This does not claim PostgreSQL compatibility.

## Local data and logs

Git ignores `.env` and `.env.*` except `.env.example`, session files/sidecars,
`var/`, local database files/sidecars, and logs. Keep private source messages,
review documents, and report exports under `var/`. Session files grant account
access; do not share or commit them.

Logs are JSON on stderr with UTC timestamps. Only fixed application event names,
numeric IDs/counts/durations, and enumerated error categories are emitted.
Unknown/free-form library logs become `unstructured_log_redacted`; arbitrary
arguments, extra fields, stack traces, and exception text are omitted. New
features must register safe event names and log only allowed metadata. Do not
log full source text or credentials. `config check` reports success or actionable
errors without displaying configuration values.

This project currently makes no live collection, accuracy, or market-coverage
claims. Future reports describe observed advertising in the configured channel,
with unknown facts kept unknown.
