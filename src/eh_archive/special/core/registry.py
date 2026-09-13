from .contracts import WorkflowDefinition

WORKFLOW_REGISTRY: dict[str, WorkflowDefinition] = {}


def register(definition: WorkflowDefinition) -> None:
    if definition.kind in WORKFLOW_REGISTRY:
        raise ValueError(f"duplicate module: {definition.kind}")
    if (
        definition.schema_version < 1
        or definition.schema_version not in definition.readable_versions
    ):
        raise ValueError("invalid module schema version")
    for name, operation in definition.operations.items():
        if name != operation.name or operation.effect not in {"readonly", "idempotent", "verify"}:
            raise ValueError("invalid operation declaration")
        if (
            operation.max_attempts < 1
            or operation.retry_delay_seconds < 0
            or (operation.lease_seconds is not None and operation.lease_seconds <= 0)
            or (operation.timeout_seconds is not None and operation.timeout_seconds <= 0)
        ):
            raise ValueError("invalid execution policy")
    WORKFLOW_REGISTRY[definition.kind] = definition


def get_workflow_definition(kind: str) -> WorkflowDefinition:
    from ..catalog import load_modules

    load_modules()
    try:
        return WORKFLOW_REGISTRY[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported special workflow kind: {kind}") from exc


def get_operation(kind: str, operation: str):
    try:
        return get_workflow_definition(kind).operations[operation]
    except KeyError as exc:
        raise ValueError(f"unsupported operation {operation!r} for workflow {kind!r}") from exc
