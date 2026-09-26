"""Public conversation API and durable SSE replay."""

import asyncio
import sqlite3

from fastapi.testclient import TestClient
from test_runtime import FakeAsk, FakeModel, QueuedScheduler

from qaneris.common.errors import ModelInvocationError
from qaneris.interfaces.api.app import create_app
from qaneris.runtime.scheduler import InlineRunScheduler


def test_conversation_routes_and_replayed_sse(tmp_path):
    app = create_app(str(tmp_path / "catalog.db"))
    runtime = app.state.run_orchestrator
    runtime.ask = FakeAsk()
    runtime.resolver.model = FakeModel()
    runtime.scheduler = InlineRunScheduler()
    with TestClient(app) as client:
        created = client.post("/api/conversations", json={"datasource_ids": []})
        assert created.status_code == 201
        conversation_id = created.json()["conversation_id"]
        assert len(client.get("/api/conversations").json()) == 1
        response = client.post(
            f"/api/conversations/{conversation_id}/runs",
            json={"question": "销售额是多少？", "client_request_id": "req"},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        again = client.post(
            f"/api/conversations/{conversation_id}/runs",
            json={"question": "销售额是多少？", "client_request_id": "req"},
        )
        assert again.json()["run_id"] == run_id
        run = client.get(f"/api/runs/{run_id}").json()
        assert run["status"] == "COMPLETED"
        assert run["response_json"]["answer"] == "销售额为 100"
        detail = client.get(f"/api/conversations/{conversation_id}").json()
        assert len(detail["messages"]) == 2
        replay = client.get(f"/api/runs/{run_id}/stream?after_sequence=3")
        assert replay.status_code == 200
        ids = [
            int(line.removeprefix("id: "))
            for line in replay.text.splitlines()
            if line.startswith("id: ")
        ]
        assert ids == list(range(4, len(runtime.runs.events(run_id)) + 1))
        assert "event: RUN_COMPLETED" in replay.text
        assert "id: 3" not in replay.text
        assert len(runtime.ask.questions) == 1


def test_delete_conversation_removes_history_and_run_state(tmp_path):
    path = tmp_path / "catalog.db"
    app = create_app(str(path))
    runtime = app.state.run_orchestrator
    runtime.ask = FakeAsk()
    runtime.resolver.model = FakeModel()
    runtime.scheduler = InlineRunScheduler()
    with TestClient(app) as client:
        conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
        run_id = client.post(
            f"/api/conversations/{conversation_id}/runs", json={"question": "销售额是多少？"}
        ).json()["run_id"]
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO diagnostic_checkpoint VALUES (?, '{}', 0)", (run_id,))
            db.execute("INSERT INTO diagnostic_task VALUES (?, ?, 1, 1, '{}', 0)", ("diag-task", run_id))
            db.execute("INSERT INTO federated_plan VALUES (?, '{}')", (run_id,))
            db.execute("INSERT INTO federated_source_task VALUES (?, ?, 1, '{}', 0)", ("fed-task", run_id))
        assert client.delete(f"/api/conversations/{conversation_id}").status_code == 204
        assert client.get("/api/conversations").json() == []
        assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
        assert client.get(f"/api/runs/{run_id}").status_code == 404
        assert client.delete(f"/api/conversations/{conversation_id}").status_code == 404
    with sqlite3.connect(path) as db:
        for table, column, value in (
            ("conversation", "id", conversation_id),
            ("conversation_message", "conversation_id", conversation_id),
            ("conversation_memory", "conversation_id", conversation_id),
            ("run", "conversation_id", conversation_id),
            ("run_event", "run_id", run_id),
            ("diagnostic_checkpoint", "run_id", run_id),
            ("diagnostic_task", "run_id", run_id),
            ("federated_plan", "run_id", run_id),
            ("federated_source_task", "run_id", run_id),
        ):
            assert db.execute(f"SELECT count(*) FROM {table} WHERE {column}=?", (value,)).fetchone()[0] == 0


def test_delete_conversation_refuses_active_run(tmp_path):
    app = create_app(str(tmp_path / "catalog.db"))
    app.state.run_orchestrator.scheduler = QueuedScheduler()
    with TestClient(app) as client:
        conversation_id = client.post("/api/conversations", json={}).json()["conversation_id"]
        client.post(f"/api/conversations/{conversation_id}/runs", json={"question": "销售额是多少？"})
        response = client.delete(f"/api/conversations/{conversation_id}")
        assert response.status_code == 409
        assert len(client.get("/api/conversations").json()) == 1


def test_observer_disconnect_does_not_cancel_run(tmp_path):
    app = create_app(str(tmp_path / "catalog.db"))
    runtime = app.state.run_orchestrator
    runtime.ask = FakeAsk()
    runtime.scheduler = QueuedScheduler()
    conversation = app.state.conversation_service.create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    runtime.runs.append_event(run.run_id, "OBSERVER_TEST", {})
    route = next(
        item for item in app.routes if getattr(item, "path", None) == "/api/runs/{run_id}/stream"
    )

    async def disconnect():
        response = await route.endpoint(run.run_id, 0, None)
        first = await anext(response.body_iterator)
        assert "id: 1" in first
        await response.body_iterator.aclose()

    asyncio.run(disconnect())
    runtime.scheduler.drain()
    assert runtime.runs.get(run.run_id).status == "COMPLETED"
    assert len(runtime.ask.questions) == 1

    async def reconnect():
        response = await route.endpoint(run.run_id, 1, None)
        frames = [frame async for frame in response.body_iterator]
        assert frames and all("id: 1\n" not in frame for frame in frames)
        assert "RUN_COMPLETED" in frames[-1]

    asyncio.run(reconnect())


def test_replay_does_not_stop_at_old_failure_while_retry_is_queued(tmp_path):
    class FlakyAsk(FakeAsk):
        def execute(self, request):
            if not self.questions:
                self.questions.append(request.question)
                raise ModelInvocationError("temporary")
            yield from super().execute(request)

    app = create_app(str(tmp_path / "catalog.db"))
    runtime = app.state.run_orchestrator
    runtime.ask = FlakyAsk()
    runtime.resolver.model = FakeModel()
    runtime.scheduler = InlineRunScheduler()
    conversation = app.state.conversation_service.create()
    run = runtime.create(conversation.conversation_id, "销售额是多少？")
    assert runtime.runs.get(run.run_id).status == "FAILED"
    runtime.scheduler = QueuedScheduler()
    runtime.retry(run.run_id)
    route = next(item for item in app.routes if getattr(item, "path", None) == "/api/runs/{run_id}/stream")

    async def observe_retry():
        response = await route.endpoint(run.run_id, 0, None)
        first = await anext(response.body_iterator)
        assert "RUN_STARTED" in first

        async def finish():
            await asyncio.sleep(0.2)
            await asyncio.to_thread(runtime.scheduler.drain)

        worker = asyncio.create_task(finish())
        remaining = [frame async for frame in response.body_iterator]
        await worker
        assert any("RUN_FAILED" in frame for frame in remaining)
        assert "RUN_COMPLETED" in remaining[-1]

    asyncio.run(observe_retry())
    assert runtime.runs.get(run.run_id).attempt == 2
