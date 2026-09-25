from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, Field

from qaneris.contracts.profile import ScanPolicy, ScanStatus

T = TypeVar("T")


class InitializationJob(BaseModel):
    id: str
    datasource_id: str
    status: ScanStatus = ScanStatus.CREATED
    policy: ScanPolicy = Field(default_factory=ScanPolicy)
    snapshot_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class InitializationExecutor(Protocol):
    def execute(self, operation: Callable[[], T]) -> T: ...


class InlineInitializationExecutor:
    """Synchronous first-version executor behind an asynchronous-ready boundary."""

    def execute(self, operation: Callable[[], T]) -> T:
        return operation()


class InitializationResult(BaseModel):
    job: InitializationJob
    output: Any | None = None
