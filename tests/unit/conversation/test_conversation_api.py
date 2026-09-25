"""Public conversation API and durable SSE replay."""

import asyncio

from fastapi.testclient import TestClient
from test_runtime import FakeAsk, FakeModel, QueuedScheduler

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
