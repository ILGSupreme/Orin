from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from common.protocol.memory_types import BaseRuntimeMemoryRequest
from common.protocol.unified_types import RuntimeMessage


class WorkOrigin(str, Enum):
    MAIN_INGRESS = "main_ingress"
    LOCAL_CORTEX_WORKER = "local_cortex_worker"


class WorkDisposition(str, Enum):
    DIRECT = "direct"
    DEFERRED = "deferred"


class WorkType(str, Enum):
    LLM = "llm"
    TOOL = "tool"
    CORTEX = "cortex"
    MEMORY = "memory"


class RoutingHints(BaseModel):
    role: WorkType | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    required_modalities: list[str] = Field(default_factory=list)
    runtime_preference: list[dict[str, Any]] = Field(default_factory=list)
    latency_preference: str = "normal"


class CanonicalTask(BaseModel):
    work_type: WorkType
    operation: str
    messages: list[RuntimeMessage] = Field(default_factory=list)
    memory_request: BaseRuntimeMemoryRequest | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    routing_hints: RoutingHints = Field(default_factory=RoutingHints)


class WorkPacket(BaseModel):
    work_id: str
    disposition: WorkDisposition = WorkDisposition.DIRECT
    task: CanonicalTask
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def origin_stage(self) -> str:
        return str(self.metadata.get("origin_stage", "unknown"))

    @property
    def work_type(self) -> WorkType:
        return self.task.work_type

    @property
    def operation(self) -> str:
        return self.task.operation

    @property
    def routing_hints(self) -> RoutingHints:
        return self.task.routing_hints

    @property
    def required_capabilities(self) -> list[str]:
        return self.routing_hints.required_capabilities

    @property
    def required_modalities(self) -> list[str]:
        return self.routing_hints.required_modalities

    @property
    def required_role(self) -> str | None:
        return self.routing_hints.role.value if self.routing_hints.role else None


class WorkRequirement(BaseModel):
    role: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    required_modalities: list[str] = Field(default_factory=list)
    protocol: str | None = None


class WorkResult(BaseModel):
    status: str  # accepted | running | completed | failed
    work_id: str
    content: list[RuntimeMessage] = Field(default_factory=list)
    backend_name: str | None = None
    backend_model: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in ("accepted", "running", "completed", "failed"):
            raise ValueError(
                "status must be one of these: [accepted, running, completed, failed]"
            )
        return value

    def sanitize_for_federation(
        self,
        *,
        expose_exact_models: bool = False,
        expose_runtime_metadata: bool = False,
    ) -> WorkResult:
        """
        Return a Federation-safe WorkResult.

        This removes internal routing/backend metadata. Exact model names and
        runtime metadata are exposed only when explicitly allowed by the caller.
        """

        metadata: dict[str, Any] = {}
        if expose_runtime_metadata:
            work_details = self.metadata.get("work_details")
            if isinstance(work_details, dict):
                metadata["work_details"] = {
                    key: value
                    for key, value in work_details.items()
                    if key in {"work_type", "operation"}
                }
        return self.model_copy(
            deep=True,
            update={
                "backend_name":None,
                "backend_model":self.backend_model if expose_exact_models else None,
                "metadata":metadata,
            }
        )


class WorkResponse(BaseModel):
    status: str
    work_id: str
