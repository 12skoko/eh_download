from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class OperationDefinition:
    name: str
    allowed_phases: frozenset[str]
    running_phase: str
    lease_seconds: int | None = None
    timeout_seconds: int | None = None
    allowed_statuses: frozenset[str] = frozenset({"active"})
    failure_phase: str = "failed"
    affects_workflow: bool = True
    cancellation: bool = False
    effect: str = "readonly"
    retryable_errors: frozenset[str] = frozenset()
    max_attempts: int = 1
    retry_delay_seconds: int = 60
    validate_input: Callable[[dict], dict] = dict


class Integration:
    """Short-transaction hooks; implementations must not perform network I/O."""

    def validate(self, session, workflow, job) -> bool:
        return True

    def changed(self, repository, workflow, job, event: str) -> None:
        pass

    def resources(self, session, workflow, job) -> tuple[str, ...]:
        return ()

    def event_subject(self, workflow) -> str | None:
        return None


@dataclass(frozen=True)
class WorkflowDefinition:
    kind: str
    label: str
    initial_phase: str
    operations: dict[str, OperationDefinition]
    schema_version: int = 1
    readable_versions: frozenset[int] = frozenset({1})
    integration: Integration = field(default_factory=Integration)
    failure_phases: dict[str, str] = field(default_factory=dict)
    create: Callable[..., Any] | None = None
    actions: dict[str, Callable[..., Any]] = field(default_factory=dict)
    migrations: dict[int, Callable[[dict], dict]] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationResult:
    phase: str
    payload: dict | None = None
    progress: dict | None = None
    status: str | None = None
    next_operation: str | None = None
    delay_seconds: int = 0
