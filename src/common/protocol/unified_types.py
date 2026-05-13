from typing import Any, Literal

from pydantic import BaseModel, Field

from common.protocol.memory_types import (
    ListMemoryClaim,
    MemoryClaim,
    MemoryEvent,
    Note,
    PromptContextRequest,
    ResolveSessionRequest,
    RetrievalRequest,
    Session,
    Summary,
    User,
)

PartType = Literal["text", "image", "audio", "video", "json", "binary"]
PartEncoding = Literal["plain", "base64"]
Visibility = Literal["user", "internal"]


MessageRole = Literal[
    "system",
    "developer",
    "user",
    "assistant",
    "tool",
    "context",
]


class ContentPart(BaseModel):
    type: PartType
    data: str
    encoding: PartEncoding = "plain"
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeMessage(BaseModel):
    role: MessageRole
    parts: list[ContentPart] = Field(default_factory=list)
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeMemoryRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    channel: str = "api"
    request: (
        Summary
        | Note
        | MemoryEvent
        | MemoryClaim
        | ListMemoryClaim
        | User
        | Session
        | RuntimeMessage
        | RetrievalRequest
        | PromptContextRequest
        | ResolveSessionRequest
    )
