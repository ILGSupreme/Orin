"""Durable agent contracts shared by Cortex and Memory."""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from common.protocol.unified_types import RuntimeMessage

Privacy = Literal["local_only", "external_allowed"]


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskSpec(AgentModel):
    id: str = Field(min_length=1, max_length=64)
    goal: str = Field(min_length=1, max_length=2000)
    criteria: str = Field(min_length=1, max_length=1000)
    context: str = Field(default="", max_length=3000)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    input_refs: list[str] = Field(default_factory=list, max_length=8)
    start_at: float | None = Field(default=None, ge=0)
    repeat_seconds: int | None = Field(default=None, ge=60)
    event_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_trigger(self):
        if self.repeat_seconds and self.event_key:
            raise ValueError("Choose a schedule or an event trigger")
        if self.event_key and self.start_at is not None:
            raise ValueError("Delayed event subscriptions are not supported")
        return self


class GoalUpdate(AgentModel):
    goal_id: str
    action: Literal["status", "result", "cancel", "resume"]
    task_id: str | None = None
    feedback: str = Field(default="", max_length=3000)


class TurnPlan(AgentModel):
    # Old plans default to a whole-turn reply, not an independent answer.
    reply_scope: Literal["turn", "independent_answer"] = "turn"
    reply: str = Field(default="", max_length=6000)
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=8)
    updates: list[GoalUpdate] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = [task.id for task in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("Task IDs must be unique")
        pending = {task.id: set(task.depends_on) for task in self.tasks}
        if any(not deps <= set(ids) for deps in pending.values()):
            raise ValueError("Unknown task dependency")
        resolved = set()
        while pending:
            ready = {key for key, deps in pending.items() if deps <= resolved}
            if not ready:
                raise ValueError("Task dependencies contain a cycle")
            resolved.update(ready)
            pending = {key: deps for key, deps in pending.items() if key not in ready}
        # A dependent outcome cannot be silently reused across monitoring cycles.
        triggered = {
            task.id for task in self.tasks if task.repeat_seconds or task.event_key
        }
        if any(
            (task.id in triggered and task.depends_on)
            or set(task.depends_on) & triggered
            for task in self.tasks
        ):
            raise ValueError(
                "Recurring/event tasks must be independent in this version"
            )
        return self


class Decision(AgentModel):
    kind: Literal["execute", "finish", "ask_user"]
    action: str = Field(default="", max_length=128)
    text: str = Field(default="", max_length=3000)
    satisfied: bool = False
    evidence: list[str] = Field(default_factory=list, max_length=32)


def decision_for_evidence(evidence_ids: list[str]) -> type[Decision]:
    """Constrain generation and validation to this decision's eligible evidence."""
    ids = tuple(sorted(set(evidence_ids)))
    if ids:
        evidence_type = list[Literal[ids]]
        evidence_field = Field(default_factory=list, max_length=32)
    else:
        evidence_type = list[str]
        # const survives the GGUF projection that strips array length bounds.
        evidence_field = Field(
            default_factory=list, max_length=0, json_schema_extra={"const": []}
        )
    return create_model(
        "EvidenceDecision",
        __base__=Decision,
        evidence=(evidence_type, evidence_field),
    )


class Observation(AgentModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    action: str
    status: Literal["completed", "failed", "user"]
    text: str = Field(max_length=4000)
    work_id: str | None = None
    cycle: int = 0


class TaskState(AgentModel):
    spec: TaskSpec
    status: Literal[
        "ready", "running", "completed", "blocked", "waiting", "failed", "cancelled"
    ] = "ready"
    answer: str = ""
    observations: list[Observation] = Field(default_factory=list, max_length=100)
    pending_action: str | None = None
    pending_work_id: str | None = None
    steps: int = 0
    cycles: int = 0
    next_wake_at: float | None = None
    action_history: list[str] = Field(default_factory=list, max_length=100)
    blocked_on_dependencies: bool = False


class GoalNotice(AgentModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: float
    text: str = Field(max_length=6000)


class Goal(AgentModel):
    id: str = Field(default_factory=lambda: "goal:" + uuid4().hex)
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    privacy: Privacy = "local_only"
    status: Literal[
        "ready",
        "running",
        "waiting",
        "needs_input",
        "completed",
        "partial",
        "failed",
        "cancelled",
    ] = "ready"
    tasks: list[TaskState] = Field(min_length=1, max_length=8)
    messages: list[RuntimeMessage] = Field(default_factory=list)
    reply: str = ""
    result: str = ""
    allocated_tokens: int = 0
    revision: int = Field(default=0, ge=0)
    updated_at: float = 0
    next_wake_at: float | None = None
    lease_owner: str | None = None
    lease_until: float = 0
    notices: list[GoalNotice] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_tasks(self):
        TurnPlan(tasks=[task.spec for task in self.tasks])
        valid_refs = {
            f"{i}:{j}"
            for i, msg in enumerate(self.messages)
            for j, _ in enumerate(msg.parts)
        }
        if any(not set(task.spec.input_refs) <= valid_refs for task in self.tasks):
            raise ValueError("Task references unavailable input parts")
        return self


class AgentLimits(AgentModel):
    max_steps_per_task: int = Field(default=6, ge=1, le=20)
    slice_seconds: float = Field(default=180, gt=0, le=900)
    token_budget: int = Field(default=32768, ge=1024, le=1_000_000)
    poll_seconds: float = Field(default=5, gt=0, le=60)
    max_active_runs: int = Field(default=4, ge=1, le=16)


class GoalQuery(AgentModel):
    namespace: str = Field(min_length=1, max_length=253)
    goal_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    due_only: bool = False


class GoalWrite(AgentModel):
    namespace: str = Field(min_length=1, max_length=253)
    goal: Goal
    expected_revision: int = Field(ge=0)
    owner: str | None = None
    release: bool = False


class GoalClaim(AgentModel):
    namespace: str = Field(min_length=1, max_length=253)
    goal_id: str
    expected_revision: int = Field(ge=1)
    owner: str = Field(min_length=1)
    lease_seconds: float = Field(gt=0, le=1000)


class GoalControl(AgentModel):
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    action: Literal["resume", "cancel", "event"]
    task_id: str | None = None
    feedback: str = Field(default="", max_length=3000)
    event_key: str | None = None
