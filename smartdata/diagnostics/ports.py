"""Model boundary for diagnostic planning and synthesis."""

from typing import Protocol

from smartdata.diagnostics.models import (
    DiagnosticDecision,
    DiagnosticPlanningContext,
    DiagnosticSynthesisContext,
)


class DiagnosticModel(Protocol):
    def propose_evidence_questions(
        self, context: DiagnosticPlanningContext
    ) -> DiagnosticDecision: ...
    def synthesize_diagnosis(self, context: DiagnosticSynthesisContext) -> str: ...
