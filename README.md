# SmartData

[English](README.md) | [简体中文](README.zh-CN.md)

SmartData is a read-only, governed business data question-answering system. Natural-language requests enter `SmartDataService.ask()`. Business questions follow intent → semantic retrieval → grounding → constrained plan → validation → read-only execution → typed result + evidence. Schema inventory questions read published structure and can receive a model-generated summary grounded in that structure.

## Current source-backed status

- Phase 1 trusted query engine is implemented. A persisted historical acceptance report records a fixed real LLM + Neo4j + SQLite suite at 18/18 expected behaviors; this is not a claim of 100% overall accuracy.
- The adapter registry contains 21 drivers. Registration is not the same as real-environment acceptance.
- FastAPI exposes `POST /api/ask`; the MCP server exposes `ask_data`.
- The unified CLI provides `ask`, `doctor`, acceptance, source lifecycle, credential, and certificate commands.
- `web/frontend/` contains the React workspace. Analyze uses durable Conversations and Runs with resumable SSE, per-message results, evidence, and controlled charts. Datasource and Excel flows remain available.
- For an already scanned selected datasource, Ask can answer schema inventory questions such as “what tables are in this database?” from its published Neo4j structure. This returns tables and fields, not table rows. A configured model also summarizes the bounded structure; the inventory remains available if that summary fails.
- Result visualization and managed credential/certificate upload are implemented. Real infrastructure acceptance and remaining external-service limits are tracked in the [RS-VIZ-02 acceptance record](docs/rs-viz-02-acceptance.md) and development roadmap.

The separate `JingJIang96200/NLQuery-Test-Dataset` repository contains fixtures and Docker definitions for 16 service databases plus a containerized SQLite target. Those fixtures are not equivalent to server deployment or SmartData real acceptance.

## CLI

```bash
smartdata doctor
smartdata doctor --json
smartdata acceptance phase1
smartdata acceptance phase1 --question-class A --repeat 3 --no-trace
smartdata acceptance ask --suite phase1 --json
smartdata source test --config datasources.json
smartdata source scan --config datasources.json --json
```

The compatibility commands `smartdata-scan`, `smartdata-api`, and `smartdata-mcp` remain available.

## Local Neo4j

```bash
export SMARTDATA_NEO4J_PASSWORD='choose-a-local-password'
docker compose up --build
```

For a local process outside Compose, configure `SMARTDATA_NEO4J_URI`, `SMARTDATA_NEO4J_USERNAME`, `SMARTDATA_NEO4J_PASSWORD`, and optionally `SMARTDATA_NEO4J_DATABASE`.

## Prepare the environment

On a fresh checkout, one command prepares everything:

```bash
python3 setup.py
```

It checks and **repairs** in order: a missing `uv`, Node or Docker, a missing `.env` and its local keys, the Python and frontend dependency trees, the Neo4j container, and the backend and frontend servers. The keys it generates are local (the Neo4j container password and the credential-store master key) and are written to `.env` with mode 600.

Exactly one thing it cannot fill in: the **model endpoint credential**, which needs a real API key. The script asks for it interactively. Without it, asking a question returns `intent_parsing_failed`, while scanning, schema inventory and the web workspace keep working.

Every check reports `ok`, `fixed` or `blocked`. The exit code is `0` when nothing is blocked, `1` when something is, and `2` for a bad argument. Re-running is idempotent: anything already in place is left alone.

Servers start as detached child processes and keep running after `setup.py` exits; their logs are in `.tools/logs/`. Stop them with `pkill -f 'uvicorn smartdata' ; pkill -f 'vite --host'`.

Common flags:

```bash
python3 setup.py --check          # report only; change nothing
python3 setup.py --json           # machine-readable result
python3 setup.py --no-start       # prepare only; do not start servers
python3 setup.py --no-prompt      # do not ask for model credentials
python3 setup.py --no-install-docker   # never install Docker automatically
```

When installing Docker, macOS downloads the official Docker Desktop DMG (~586 MB, resumable, copied to `/Applications`) and Linux runs Docker's documented script (needs root, so it also requires `--allow-root`). An installed-but-stopped daemon is only reported via `open -a Docker`; the script does not launch a GUI app for you.

If a Neo4j data volume already exists its password was fixed at initialisation. The script reports this instead of silently deleting your graph data.

## Open the Web workspace

From the repository root, run one command:

```bash
./start-web.sh
```

The script loads the local `.env` when present, starts FastAPI and Vite on `127.0.0.1:8000` and `127.0.0.1:5173`, waits for both services, and opens the browser. Press Ctrl+C to stop the processes it started. Asking questions requires a reachable Neo4j and model service. Analyze creates a Conversation with a fixed datasource scope, submits each question as a Run, and observes `GET /api/runs/{run_id}/stream?after_sequence=N`. Reopening a Conversation restores messages and the latest Run without executing the query again. Older results load on demand. `POST /api/ask` and `POST /api/ask/stream` remain available for legacy clients, CLI, MCP, and Skill.

Datasource records and connection profiles are stored in the SQLite catalog selected by `SMARTDATA_CATALOG` (default: `smartdata.db` in the process working directory). Uploaded Excel data is stored in the server artifact directory; managed credentials are stored separately under `SMARTDATA_SECRET_STORE_DIR`. The datasource page can check a saved connection without changing its scan state. A successful scan records metadata, but does not guarantee that the source is still reachable later.

To add a database in the Web workspace, select a driver, fill the Basic / SSL / Advanced tabs, and run **Test connection**. After a successful test, **Save and scan** persists the same validated connection profile and scans the datasource. If scanning fails, the saved datasource remains visible and the scan can be retried.

Secure scan configs use `SecureDatasourceCreate`: passwords and certificates are `SecretReference` objects, never plaintext values. The source supports `managed`, `environment`, and `file` secret providers. Before creating managed credentials or uploading certificates in the Web workspace, configure `SMARTDATA_SECRET_STORE_DIR` outside the repository and a stable `SMARTDATA_MASTER_KEY` containing a URL-safe Base64 encoded 32-byte key, then restart the backend.

## API / MCP

```bash
smartdata-api
smartdata-mcp
```

API documentation is available at `http://127.0.0.1:8000/docs` when the API is running. The project Skill is at [`skill/SKILL.md`](skill/SKILL.md).

## Documentation

Start with [`docs/README.md`](docs/README.md). Current progress and Roadshow scope are in [`docs/development-roadmap.md`](docs/development-roadmap.md); product surfaces are in [`docs/product-interfaces.md`](docs/product-interfaces.md); connection and TLS design are in [`docs/connection-model.md`](docs/connection-model.md).

## Development

```bash
pytest
ruff check .
```

Federated analytics supports governed merge operations and confirmed cross-source key mappings. New product surfaces must not bypass grounding, validation, revision fencing, or read-only execution.
