"""One-pass application capability over the existing Ask pipeline."""

from collections.abc import Iterator
from typing import Protocol

from qaneris.application.service import QanerisService, _AskEventRecord
from qaneris.contracts import AskRequest


class AskCapability(Protocol):
    def execute(self, request: AskRequest) -> Iterator[_AskEventRecord]: ...


class ServiceAskCapability:
    def __init__(self, service: QanerisService):
        self.service = service

    def execute(self, request: AskRequest) -> Iterator[_AskEventRecord]:
        return self.service._ask_event_records(request)
