from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from common.jobs import JobSpec
from common.protocol.unified_types import RuntimeMessage


class InferenceSession(BaseModel):
    user_id: str
    session_id: str | None = None
    channel: str = "api"
    stream: bool = False
    content: list[RuntimeMessage]
    metadata: dict[str, Any] = Field(default_factory=dict)


class LoadModelRequest(BaseModel):
    model_id: str
    provider: str = "huggingface"
    engine: str | None = None
    repo_id: str | None = None
    filename: str | None = None
    revision: str = "main"
    tokenizer_id: str | None = None
    force_reload: bool = False

    def to_job_spec(self) -> JobSpec:
        return JobSpec(
            kind="load_model",
            payload={
                "model_id": self.model_id,
                "provider": self.provider or "huggingface",
                "engine": self.engine,
                "repo_id": self.repo_id,
                "filename": self.filename,
                "revision": self.revision,
                "tokenizer_id": self.tokenizer_id,
                "force_reload": self.force_reload,
            },
        )


class LoadBackendRequest(BaseModel):
    engine: Literal["vllm", "gguf"]


def is_terminal_command(req: InferenceSession) -> bool:
    latest_user_message = get_latest_message(req)

    if not latest_user_message:
        return False

    if latest_user_message.data.startswith("/"):
        return True

    return False


def get_latest_message(req: InferenceSession):
    runtime_message = req.content[0] if req.content else None
    if not runtime_message:
        return None
    message = runtime_message.parts[0] if runtime_message.parts else None

    if not message:
        return None

    if message.type == "text":
        return message
    else:
        return None
