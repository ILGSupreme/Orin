"""Serializable contracts for bounded, local Cortex agent runs."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecuteAction(StrictModel):
    decision: Literal["execute_action"]
    action: Literal["cluster_snapshot", "list_nodes", "list_pods", "analyze", "summarize"]
    text: str = Field(default="", max_length=2000)


class Finish(StrictModel):
    decision: Literal["finish"]
    satisfied: bool
    answer: str = Field(min_length=1, max_length=4000)
    evidence: list[int] = Field(default_factory=list)


class AskUser(StrictModel):
    decision: Literal["ask_user"]
    question: str = Field(min_length=1, max_length=1000)


AgentDecision = Annotated[ExecuteAction | Finish | AskUser, Field(discriminator="decision")]
DECISION_ADAPTER = TypeAdapter(AgentDecision)


class AgentLimits(StrictModel):
    max_steps: int = Field(default=6, ge=1, le=20)
    timeout_seconds: float = Field(default=180, gt=0, le=900)
    token_budget: int = Field(default=16384, ge=1, le=65536)
    max_action_repeats: int = Field(default=1, ge=1, le=3)


class Observation(StrictModel):
    step: int
    action: str
    work_id: str
    status: Literal["completed", "failed"]
    text: str


class AgentState(StrictModel):
    run_id: str
    goal: str
    completion_criteria: str
    outcome: Literal["running", "completed", "partial", "awaiting_user", "exhausted", "failed", "cancelled"] = "running"
    steps: int = 0
    reserved_tokens: int = 0
    pending_action: str | None = None
    pending_work_id: str | None = None
    observations: list[Observation] = Field(default_factory=list)
    answer: str = ""
