from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str
    content_type: str = "text"
    images: Optional[List[str]] = None


class IngressRequest(BaseModel):
    user_id: str
    session_id: str | None = None
    channel: str = "api"
    messages: List[ChatMessage]
    mode: str = Field(default="auto", description="auto|fast|smart|vision")
    stream: bool = False
    temperature: float = 0.7
    max_tokens: int = 512
    num_ctx: int = 4096
    work_id: str

    def get_messages_to_dict(self) -> list[dict[str, Any]]:
        return [m.model_dump() for m in self.messages]


class NormalizedIngress(BaseModel):
    request: IngressRequest
    resolved_session_id: str


class EgressResponse(BaseModel):
    content: str
    session_id: str | None = None
    work_id: str | None = None
    backend: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LoadModelRequest(BaseModel):
    model_id: str
    force_reload: bool = False

    repo_id: str | None = None
    filename: str | None = None
    revision: str = "main"
    tokenizer_id: str | None = None


class LoadBackendRequest(BaseModel):
    backend: Literal["vllm", "gguf"]
