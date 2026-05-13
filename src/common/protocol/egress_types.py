from __future__ import annotations

from pydantic import BaseModel, Field

from common.protocol.unified_types import RuntimeMessage


class EgressResponse(BaseModel):
    content: list[RuntimeMessage]
    work_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
