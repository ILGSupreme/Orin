from typing import Any, Literal

from pydantic import BaseModel, Field

MemoryType = Literal["configuration", "preference", "plan", "profile"]
MemoryPolicy = Literal["explicit_only", "important_only"]
SessionStatus = Literal["active", "closed"]
ChatRole = Literal["user", "assistant", "system"]
ContentType = Literal["text", "voice_transcript", "image_caption"]


class Summary(BaseModel):
    topic: str
    summary: str


class Note(BaseModel):
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)


class MemoryEvent(BaseModel):
    raw_text: str
    source: str = "user"


class MemoryClaim(BaseModel):
    memory_type: MemoryType
    subject: str
    predicate: str
    value: Any
    confidence: float = 1.0
    source: str = ""
    source_event_id: int | None = None


class ListMemoryClaim(BaseModel):
    subject: str


class User(BaseModel):
    external_id: str
    display_name: str | None = None
    memory_policy: MemoryPolicy = "explicit_only"


class Session(BaseModel):
    user_id: str
    channel: str
    status: SessionStatus = "active"


class RetrievalRequest(BaseModel):
    query: str
    limit: int = Field(default=6, ge=1, le=20)
    include_claims: bool = True
    include_summaries: bool = True
    include_recent_messages: bool = True


class PromptContextRequest(BaseModel):
    query: str
    limit: int = Field(default=6, ge=1, le=20)
    include_recent_messages: bool = True
    recent_message_limit: int | None = None
    recent_message_roles: list[str] = Field(default_factory=list)
    include_claims: bool = True
    include_summaries: bool = True
    include_chunks: bool = True


class BasePromptContextResponse(BaseModel):
    recent_messages: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    summaries: list[dict[str, Any]]
    retrieved_chunks: list[dict[str, Any]]
    prompt_block: str


class ResolveSessionRequest(BaseModel):
    max_idle_minutes: int = 60


class BaseRuntimeMemoryRequest(BaseModel):
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
        | dict[str, Any]
        | RetrievalRequest
        | PromptContextRequest
        | ResolveSessionRequest
    )
