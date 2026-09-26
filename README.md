# Qaneris

[English](README.md) | [简体中文](README.zh-CN.md)

Qaneris is a read-only, governed business data question-answering system. Natural-language requests enter `QanerisService.ask()`. Business questions follow intent → semantic retrieval → grounding → constrained plan → validation → read-only execution → typed result + evidence. Schema inventory questions read published structure and can receive a model-generated summary grounded in that structure.

## Current source-backed status

- Phase 1 trusted query engine is implemented. A persisted historical acceptance report records a fixed real LLM + Neo4j + SQLite suite at 18/18 expected behaviors; this is not a claim of 100% overall accuracy.
- The adapter registry contains 21 drivers. Registration is not the same as real-environment acceptance.
- FastAPI exposes `POST /api/ask`; the MCP server exposes `ask_data`.
- The unified CLI provides `ask`, `doctor`, acceptance, source lifecycle, credential, and certificate commands.
- `web/frontend/` contains the React workspace. Analyze uses durable Conversations and Runs with resumable SSE, per-message results, evidence, and controlled charts. Datasource and Excel flows remain available.
- For an already scanned selected datasource, Ask can answer schema inventory questions such as “what tables are in this database?” from its published Neo4j structure. This returns tables and fields, not table rows. A configured model also summarizes the bounded structure; the inventory remains available if that summary fails.
- Result visualization and managed credential/certificate upload are implemented. Real infrastructure acceptance and remaining external-service limits are tracked in the [RS-VIZ-02 acceptance record](docs/rs-viz-02-acceptance.md) and development roadmap.

The separate `JingJIang96200/NLQuery-Test-Dataset` repository contains fixtures and Docker definitions for 16 service databases plus a containerized SQLite target. Those fixtures are not equivalent to server deployment or Qaneris real acceptance.

## CLI

The CLI is a local orchestration layer: every command calls only public `QanerisService` methods. It is not an HTTP client, and it never reads the catalog, the credential store, the certificate validator, an adapter or Neo4j directly. CLI, API, MCP, Skill and Web therefore share one set of product rules.

### Install and entry points

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[sql,neo4j]'   # core commands
.venv/bin/python -m pip install -e '.[shell]'       # optional: interactive session
```

Four entry points: `qaneris` (the main command), `qaneris-shell` (straight into the interactive session), `qaneris-api` and `qaneris-mcp`.

Commands resolve `.env` against the **current directory** (override with the global `--env-file FILE` or `QANERIS_ENV_FILE`), and datasource records live in the SQLite catalog named by `QANERIS_CATALOG` (default: `qaneris.db` in the current directory). Run from one fixed directory, or you will address a different catalog.

### Interactive session

```bash
qaneris            # the same session: a bare invocation opens it directly
qaneris shell
```

An inline TUI: the banner and a persistent status line are drawn by the terminal UI, while each command's own output stays in the terminal's normal scrollback, so it can be scrolled, copied and searched as usual. Every line is handed to the same entry point the one-shot commands use, so output, exit codes and error rules are identical.

- Session builtins are `exit` / `quit` / `help` / `clear` (with `?` as an alias for `help`), honoured only when the word is not a registered command, so they never shadow the command tree.
- A leading `qaneris` is stripped from each line, so a command copied from a README (`qaneris source list`) works unchanged; typing the program name alone prints the help text. Typing `shell` inside the session prints `shell`'s help instead of nesting a second session.
- A command's exit code is shown on the status line and is **not** the session's exit code; a session that ends normally returns 0.
- Ctrl-C cancels the current input line without ending the session; Ctrl-D exits.
- A non-terminal stdin is always refused (exit code 2), so a script cannot enter an interactive session by accident.
- The banner adapts to the terminal width (sections sit side by side when they fit, and stack when they do not), and the status line colours a command's exit code by meaning: green for 0, red for 1 or 2.
- Command history is written to `.tools/shell_history` (directory 0700 / file 0600, already git-ignored); disable it with `--no-history` or move it with `--history FILE`.

```bash
qaneris --no-banner              # skip the welcome panel
qaneris --no-history             # keep no history
qaneris --history ~/.qaneris_history
```

A bare `qaneris` opens this session rather than printing a usage error, and so does `qaneris` with
only session options, so the two forms below are equivalent. A named command, and `--help`, keep
the one-shot entry point.

```bash
qaneris            # opens the session
qaneris shell      # opens the same session
```

### Command reference

```text
qaneris doctor [--json]
qaneris ask QUESTION [--workspace ID] [--datasource ID] [--max-rows N] [--stream] [--json]

qaneris source list [--workspace ID] [--json]
qaneris source show DATASOURCE_ID [--json]
qaneris source scan-one DATASOURCE_ID [--json]
qaneris source test-one --config FILE [--json]
qaneris source create --config FILE [--json]
qaneris source update DATASOURCE_ID --config FILE [--json]
qaneris source delete DATASOURCE_ID [--json]
qaneris source test --config FILE [--json]
qaneris source scan --config FILE [--json]
qaneris source import-excel FILE [--name NAME] [--workspace ID] [--json]

qaneris credential add --kind {password|token|api_key|client_private_key_password} [--stdin] [--json]
qaneris credential show SECRET_ID [--json]
qaneris credential delete SECRET_ID [--json]

qaneris certificate add-ca FILE [--json]
qaneris certificate add-client --certificate FILE --private-key FILE
                              [--private-key-password-prompt | --private-key-password-stdin] [--json]
qaneris certificate inspect SECRET_ID [--json]
qaneris certificate delete SECRET_ID [--json]

qaneris acceptance phase1 [--question-class {A,B,C,D,E,F}] [--repeat N] [--no-trace] [--json]
qaneris acceptance ask --suite phase1 [...same options]

qaneris [--history FILE | --no-history] [--no-banner]   # bare form: opens the session
qaneris shell [--history FILE | --no-history] [--no-banner]
```

### Self-check and acceptance

`qaneris doctor` checks the artifact root, model configuration, Neo4j configuration and connectivity, and catalog configuration. It publishes status only and never exposes credential contents.

`qaneris acceptance phase1` replays the fixed six-class suite (`--question-class A-F` selects one, `--repeat N` repeats, `--no-trace` omits the trace). It is a **reproduction tool for the historical baseline**: the recorded 18/18 is a fixed question set measured against a real LLM + Neo4j + SQLite environment, not the system's overall accuracy.

### Asking questions

`qaneris ask` is a non-interactive command: one command → one `AskRequest` → one `AskResponse`. `--datasource` accepts a datasource id only (a name is never guessed); the legal range of `--max-rows` is owned by the `AskRequest` contract.

| Command | stdout | Exit code |
| --- | --- | --- |
| `ask "Q"` | Human-readable: Status / Datasource / Scan version / Rows / Truncated / result table / safe query display / short plan. `clarification_required` prints the clarification; a failure prints only its stable error code, with no traceback | 0 / 1 |
| `ask "Q" --json` | Exactly one JSON document: a stable field projection of `AskResponse` | 0 / 1 |
| `ask "Q" --stream` | One line per real `AskEvent`, as `[stage] headline (facts...)`; no invented stage, no private model reasoning, no secret | 0 / 1 |
| `ask "Q" --stream --json` | Strict JSONL, one serialized event per line, with `done` always last | 0 / 1 |

A clarification (`clarification_required`) is a normal product conclusion and exits 0, exactly as the API and MCP return it.

```bash
qaneris ask "What was total collected revenue?" --datasource ds_c996e9437e6a
qaneris ask "What tables are in this database?" --datasource ds_c996e9437e6a
qaneris ask "How many orders last month?" --stream
```

### Datasource lifecycle

The secure path is `Test → Save → Scan` with a `SecureDatasourceCreate` config:

```bash
qaneris source test-one --config datasource.json     # test only; save nothing, scan nothing, write no graph
qaneris source create --config datasource.json       # test and save; stops at created, no scan
qaneris source scan-one ds_c996e9437e6a              # scan and publish the structure explicitly
```

Neither `create` nor `update` scans automatically, so a created datasource needs an explicit `scan-one`. `source list` / `show` / `scan-one` publish only public fields (ID / Name / Kind / Driver / Status / Workspace and scan state); a stored connection document, a secret reference or any credential is never among them.

The config format is identical to the batch `source test` / `scan` files and must describe exactly one datasource (several are refused rather than silently picked):

```json
{
  "name": "retail-mongodb",
  "kind": "document",
  "workspace_id": "default",
  "connection_profile": {
    "driver": "mongodb",
    "deployment_mode": "local",
    "endpoint": { "url": "mongodb://127.0.0.1:27018", "database": "retail" },
    "authentication": { "method": "none" }
  }
}
```

A password is a secret reference, never a plaintext value; `provider` supports `managed`, `environment` and `file`:

```json
"authentication": {
  "method": "password",
  "username": "readonly",
  "password": { "provider": "environment", "identifier": "QANERIS_PG_PASSWORD" }
}
```

**Inline secrets in a config file are refused** (a plaintext `password` / `token`, or a password embedded in a URL, is an error), so use a `SecretReference`. More examples live in `configs/acceptance/local/`.

### Credentials and certificates

Before creating a managed credential, give the **process** a store directory and a master key:

```dotenv
QANERIS_SECRET_STORE_DIR=/path/outside/Qaneris/managed-secrets
QANERIS_MASTER_KEY=<URL-safe Base64 of a 32-byte random key>
```

The store directory must be outside the repository, and the master key must be kept stable rather than regenerated on every start. A missing configuration returns `credential_store_configuration_error`.

**A secret never enters argv**: the command tree has no `--password VALUE`, `--token VALUE`, `--api-key VALUE`, `--secret VALUE`, `--value VALUE` or `--private-key-password VALUE`. There are exactly two input channels:

```bash
qaneris credential add --kind password                      # hidden interactive input (getpass, no echo)
printf '%s' "$PG_PASSWORD" | qaneris credential add --kind password --stdin   # explicit pipe
```

A pipe must say `--stdin` explicitly, otherwise it is refused (so a redirected file is never read as a password by accident). `credential add --kind` accepts text kinds only; `ca_certificate`, `client_certificate` and `client_private_key` must go through the `certificate` commands and cannot bypass the file boundary via `--kind`.

### Excel import

```bash
qaneris source import-excel orders.xlsx --name "Orders 2026" --workspace default
```

Excel is an **ingestion source**, not a second query engine: a workbook is deterministically validated and materialized into an immutable SQLite database, after which the same Scan → Graph → Grounding → Query chain applies. Exit code 0 is reachable only for a READY import, which includes a verified Neo4j publication.

### Exit codes and output rules

| Exit code | Meaning |
| --- | --- |
| `0` | The command completed (including `clarification_required`, which is a product conclusion) |
| `1` | The command ran and failed (connection failure, product error, runtime exception) |
| `2` | Usage or configuration error (invalid arguments, unparsable config file) |

- With `--json`, stdout carries a **single JSON document**; incidental driver and provider output is redirected to stderr, so JSON purity does not depend on the drivers being quiet.
- With `--stream --json`, stdout carries events only, with `done` always last.
- An unexpected exception exposes only its type, never an arbitrary message that could carry a credential.

## Local Neo4j

```bash
export QANERIS_NEO4J_PASSWORD='choose-a-local-password'
docker compose up --build
```

For a local process outside Compose, configure `QANERIS_NEO4J_URI`, `QANERIS_NEO4J_USERNAME`, `QANERIS_NEO4J_PASSWORD`, and optionally `QANERIS_NEO4J_DATABASE`.

## Prepare the environment

On a fresh checkout, one command prepares everything:

```bash
python3 setup.py
```

It checks and **repairs** in order: a missing `uv`, Node or Docker, a missing `.env` and its local keys, the Python and frontend dependency trees, the Neo4j container, and the backend and frontend servers. The keys it generates are local (the Neo4j container password and the credential-store master key) and are written to `.env` with mode 600. When Docker is installed but its daemon is not running, Docker Desktop is launched and waited for; once the workspace answers, it opens in your browser.

Exactly one thing it cannot fill in: the **model endpoint credential**, which needs a real API key. The script asks for it interactively. Without it, asking a question returns `intent_parsing_failed`, while scanning, schema inventory and the web workspace keep working.

Every check reports `ok`, `fixed` or `blocked`. The exit code is `0` when nothing is blocked, `1` when something is, and `2` for a bad argument. Re-running is idempotent: anything already in place is left alone.

Servers start as detached child processes and keep running after `setup.py` exits; their logs are in `.tools/logs/`. Stop them with `pkill -f 'uvicorn qaneris' ; pkill -f 'vite --host'`.

Common flags:

```bash
python3 setup.py --check          # report only; change nothing
python3 setup.py --json           # machine-readable result
python3 setup.py --no-start       # prepare only; do not start servers
python3 setup.py --no-prompt      # do not ask for model credentials
python3 setup.py --no-install-docker   # never install Docker automatically
python3 setup.py --no-start-docker     # do not launch Docker Desktop for a stopped daemon
python3 setup.py --no-open             # do not open the browser when the workspace is ready
```

When installing Docker, macOS downloads the official Docker Desktop DMG (~586 MB, resumable, copied to `/Applications`) and Linux runs Docker's documented script (needs root, so it also requires `--allow-root`). Docker Desktop is only auto-installed where the vendor publishes a single documented artifact; elsewhere the script prints the download link. An installed-but-stopped daemon on macOS is launched with `open -a Docker` and waited for (up to 180 s, including the first-run licence prompt); pass `--no-start-docker` to get the instruction instead. On Linux, dockerd needs root, so it is never started silently.

If a Neo4j data volume already exists its password was fixed at initialisation. The script reports this instead of silently deleting your graph data.

The credential store lives outside the repository (by default `~/.qaneris/secrets`), so every checkout on one machine shares it. A new `.env` therefore only generates a master key while that store is empty; if it already holds encrypted credentials, the script leaves the key blank and asks you to restore the original, because a fresh key would make them undecryptable.

## Open the Web workspace

From the repository root, run one command:

```bash
./start-web.sh
```

The script loads the local `.env` when present, starts FastAPI and Vite on `127.0.0.1:8000` and `127.0.0.1:5173`, waits for both services, and opens the browser. Press Ctrl+C to stop the processes it started. Asking questions requires a reachable Neo4j and model service. Analyze creates a Conversation with a fixed datasource scope, submits each question as a Run, and observes `GET /api/runs/{run_id}/stream?after_sequence=N`. Reopening a Conversation restores messages and the latest Run without executing the query again. Older results load on demand. `POST /api/ask` and `POST /api/ask/stream` remain available for legacy clients, CLI, MCP, and Skill.

Datasource records and connection profiles are stored in the SQLite catalog selected by `QANERIS_CATALOG` (default: `qaneris.db` in the process working directory). Uploaded Excel data is stored in the server artifact directory; managed credentials are stored separately under `QANERIS_SECRET_STORE_DIR`. The datasource page can check a saved connection without changing its scan state. A successful scan records metadata, but does not guarantee that the source is still reachable later.

To add a database in the Web workspace, select a driver, fill the Basic / SSL / Advanced tabs, and run **Test connection**. After a successful test, **Save and scan** persists the same validated connection profile and scans the datasource. If scanning fails, the saved datasource remains visible and the scan can be retried.

Secure scan configs use `SecureDatasourceCreate`: passwords and certificates are `SecretReference` objects, never plaintext values. The source supports `managed`, `environment`, and `file` secret providers. Before creating managed credentials or uploading certificates in the Web workspace, configure `QANERIS_SECRET_STORE_DIR` outside the repository and a stable `QANERIS_MASTER_KEY` containing a URL-safe Base64 encoded 32-byte key, then restart the backend.

## API / MCP

```bash
qaneris-api     # then open http://127.0.0.1:8000/docs
qaneris-mcp
```

The main endpoints are `POST /api/ask`, `POST /api/ask/stream` and the datasource, credential and certificate interfaces. The compatibility command `qaneris-scan` remains available. The project Skill is at [`skill/SKILL.md`](skill/SKILL.md).

## Documentation

Start with [`docs/README.md`](docs/README.md). Current progress and Roadshow scope are in [`docs/development-roadmap.md`](docs/development-roadmap.md); product surfaces are in [`docs/product-interfaces.md`](docs/product-interfaces.md); connection and TLS design are in [`docs/connection-model.md`](docs/connection-model.md).

## Development

```bash
pytest
ruff check .
```

Federated analytics supports governed merge operations and confirmed cross-source key mappings. New product surfaces must not bypass grounding, validation, revision fencing, or read-only execution.
