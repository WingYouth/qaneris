"""The sole authority for run status transitions."""

from smartdata.runtime.models import Run, RunStatus

ALLOWED: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.CONTEXTUALIZING, RunStatus.CANCELLED},
    RunStatus.CONTEXTUALIZING: {
        RunStatus.DISCOVERING,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.CANCELLED,
    },
    RunStatus.DISCOVERING: {
        RunStatus.PLANNING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.CANCELLED,
    },
    RunStatus.PLANNING: {
        RunStatus.EXECUTING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.CANCELLED,
    },
    RunStatus.EXECUTING: {
        RunStatus.ANSWERING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.CANCELLED,
    },
    RunStatus.ANSWERING: {
        RunStatus.COMPLETED,
        RunStatus.WAITING_USER,
        RunStatus.FAILED,
        RunStatus.BLOCKED,
        RunStatus.CANCELLED,
    },
    RunStatus.WAITING_USER: {RunStatus.CONTEXTUALIZING, RunStatus.CANCELLED},
    RunStatus.FAILED: {RunStatus.CONTEXTUALIZING},
    RunStatus.COMPLETED: set(),
    RunStatus.CANCELLED: set(),
    RunStatus.BLOCKED: set(),
}


def transition(run: Run, target: RunStatus, **changes: object) -> Run:
    if target not in ALLOWED[run.status]:
        raise ValueError(f"invalid run transition: {run.status} -> {target}")
    if run.status == RunStatus.FAILED and not run.retryable:
        raise ValueError("run failure is not retryable")
    return run.model_copy(
        update={"status": target, "current_stage": target.value.lower(), **changes}
    )
