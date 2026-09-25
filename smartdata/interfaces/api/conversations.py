"""Conversation and durable run routes."""

import asyncio
import json
from time import monotonic
from typing import Annotated

from fastapi import FastAPI, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from smartdata.application.service import SmartDataService
from smartdata.capabilities.ask import ServiceAskCapability
from smartdata.conversation.context import ConversationContextResolver
from smartdata.conversation.repository import SQLiteConversationRepository
from smartdata.conversation.service import ConversationService
from smartdata.runtime.models import RunStatus
from smartdata.runtime.orchestrator import RunOrchestrator
from smartdata.runtime.repository import SQLiteRunRepository
from smartdata.runtime.scheduler import ThreadRunScheduler


class ConversationCreate(BaseModel):
    workspace_id: str = "default"
    title: str = ""
    datasource_ids: list[str] = Field(default_factory=list)


class RunCreate(BaseModel):
    question: str = Field(min_length=1)
    max_rows: int = Field(default=200, ge=1, le=1000)
    client_request_id: str | None = None


class ClarificationReply(BaseModel):
    answer: str = Field(min_length=1)


class JoinMappingCreate(BaseModel):
    workspace_id: str = "default"
    business_key: str
    left_datasource_id: str
    left_data_object_id: str
    left_field_path: str
    left_grain: str
    right_datasource_id: str
    right_data_object_id: str
    right_field_path: str
    right_grain: str
    cardinality: str
    null_policy: str
    source: str = "manual"
    confidence: float = Field(default=1.0, ge=0, le=1)


class JoinMappingConfirm(BaseModel):
    confirmed_by: str = Field(min_length=1)


def register_conversation_routes(
    app: FastAPI, service: SmartDataService, database_path: str
) -> RunOrchestrator:
    conversations = SQLiteConversationRepository(database_path)
    runs = SQLiteRunRepository(database_path)
    conversation_service = ConversationService(conversations)
    runtime = RunOrchestrator(
        conversations,
        runs,
        ServiceAskCapability(service),
        ConversationContextResolver(getattr(service, "model", None)),
        ThreadRunScheduler(),
        federation_service=service,
    )
    app.state.conversation_service = conversation_service
    app.state.run_orchestrator = runtime

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: ConversationCreate):
        return conversation_service.create(body.workspace_id, body.title, body.datasource_ids)

    @app.get("/api/conversations")
    def list_conversations(workspace_id: str = Query("default")):
        return conversations.list(workspace_id)

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(conversation_id: str):
        return conversation_service.detail(conversation_id)

    @app.post("/api/conversations/{conversation_id}/runs", status_code=202)
    def create_run(conversation_id: str, body: RunCreate):
        run = runtime.create(conversation_id, body.question, body.max_rows, body.client_request_id)
        return {
            "run_id": run.run_id,
            "conversation_id": run.conversation_id,
            "status": run.status,
            "run_kind": run.run_kind,
            "created_at": run.created_at,
        }

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        run = runs.get(run_id)
        if run.run_kind != "federated":
            return run
        payload = run.model_dump(mode="json")
        plan = runtime.federation.repository.plan(run_id)
        if plan:
            payload["federated_plan"] = {
                "plan_id": plan.plan_id,
                "merge_operation": plan.merge_plan.operation,
                "datasource_ids": [task.datasource_id for task in plan.source_tasks],
            }
            payload["source_tasks"] = [{
                "task_id": task.task_id, "datasource_id": task.datasource_id,
                "status": task.status, "attempt": task.attempt,
            } for task in runtime.federation.repository.tasks(run_id)]
        return payload

    @app.get("/api/join-mappings")
    def list_join_mappings(workspace_id: str = Query("default")):
        return runtime.federation.repository.mappings(workspace_id)

    @app.post("/api/join-mappings", status_code=201)
    def create_join_mapping(body: JoinMappingCreate):
        return runtime.federation.governance.create(**body.model_dump())

    @app.post("/api/join-mappings/{mapping_id}/confirm")
    def confirm_join_mapping(mapping_id: str, body: JoinMappingConfirm):
        return runtime.federation.governance.confirm(mapping_id, body.confirmed_by)

    @app.post("/api/join-mappings/{mapping_id}/reject")
    def reject_join_mapping(mapping_id: str):
        return runtime.federation.governance.reject(mapping_id)

    @app.get("/api/runs/{run_id}/stream")
    async def stream_run(
        run_id: str,
        after_sequence: int = Query(0, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ):
        runs.get(run_id)
        cursor = after_sequence
        if last_event_id and after_sequence == 0:
            try:
                cursor = max(0, int(last_event_id))
            except ValueError:
                cursor = 0

        async def observe():
            nonlocal cursor
            terminal_seen_at: float | None = None
            while True:
                events = await asyncio.to_thread(runs.events, run_id, cursor)
                for event in events:
                    cursor = event.sequence
                    yield (
                        f"id: {event.sequence}\nevent: {event.event_type}\n"
                        f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                    )
                current = await asyncio.to_thread(runs.get, run_id)
                status = current.status
                # Clarification and retry keep the same run id. A queued resume still
                # carries the previous settled status until its worker starts.
                if (status == RunStatus.FAILED and current.current_stage == "retry_queued") or (
                    status == RunStatus.WAITING_USER and current.current_stage == "resume_queued"
                ):
                    await asyncio.sleep(0.1)
                    continue
                if status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                    RunStatus.BLOCKED,
                    RunStatus.WAITING_USER,
                }:
                    terminal_event = {
                        RunStatus.COMPLETED: "RUN_COMPLETED",
                        RunStatus.FAILED: "RUN_FAILED",
                        RunStatus.CANCELLED: "RUN_CANCELLED",
                        RunStatus.BLOCKED: "RUN_BLOCKED",
                        RunStatus.WAITING_USER: "CLARIFICATION_REQUIRED",
                    }[status]
                    last = await asyncio.to_thread(runs.last_event, run_id)
                    if last and last.event_type == terminal_event and last.sequence <= cursor:
                        break
                    if terminal_seen_at is None:
                        terminal_seen_at = monotonic()
                    if monotonic() - terminal_seen_at > 5:
                        break
                await asyncio.sleep(0.1)

        return StreamingResponse(
            observe(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/clarification")
    def clarify_run(run_id: str, body: ClarificationReply):
        return runtime.clarify(run_id, body.answer)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        return runtime.cancel(run_id)

    @app.post("/api/runs/{run_id}/retry")
    def retry_run(run_id: str):
        return runtime.retry(run_id)

    return runtime
