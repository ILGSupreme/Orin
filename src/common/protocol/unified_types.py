import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from common.protocol.memory_types import BasePromptContextResponse

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

    @model_validator(mode="after")
    def set_mime_type(self) -> "ContentPart":
        # Access instance values using 'self'
        if not self.mime_type:
            self.mime_type = f"{self.type}/{self.encoding}"
        return self


class RuntimeMessage(BaseModel):
    role: MessageRole
    parts: list[ContentPart] = Field(default_factory=list)
    name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromptContextResponse(BasePromptContextResponse):
    def get_latest_message(self) -> RuntimeMessage | None:
        if not self.recent_messages:
            return None

        message = self.recent_messages[-1]
        if isinstance(message, dict):
            return RuntimeMessage.model_validate(message)

        if isinstance(message, str):
            return RuntimeMessage(
                role="assistant",
                parts=[
                    ContentPart(
                        type="text",
                        data=message,
                        encoding="plain",
                    )
                ],
            )
        return None


def egest_content_part(
    content: str,
    content_type: PartType,
    encoding: PartEncoding,
    metadata: dict[str, Any],
):
    return ContentPart(
        type=content_type, data=content, encoding=encoding, metadata=metadata
    )


def egest_runtime_message(
    role: MessageRole,
    parts: list[ContentPart],
    name: str | None,
    metadata: dict[str, Any],
):
    return RuntimeMessage(role=role, parts=parts, name=name, metadata=metadata)


def egest_text_runtime_message(
    role: MessageRole, content: str, metadata: dict[str, Any]
):
    return egest_runtime_message(
        role=role,
        parts=[
            egest_content_part(
                content=content, content_type="text", encoding="plain", metadata={}
            )
        ],
        name=None,
        metadata=metadata,
    )


def embed_system_message_user_dict(
    system_text: str, user_payload: dict
) -> list[RuntimeMessage]:
    return [
        RuntimeMessage(
            role="system",
            parts=[
                ContentPart(
                    type="text",
                    data=system_text,
                    encoding="plain",
                )
            ],
        ),
        RuntimeMessage(
            role="user",
            parts=[
                ContentPart(
                    type="text",
                    data=json.dumps(user_payload, ensure_ascii=False),
                    encoding="plain",
                )
            ],
        ),
    ]


def embed_system_message_user_text(
    system_text: str, user_text: str
) -> list[RuntimeMessage]:
    return [
        RuntimeMessage(
            role="system",
            parts=[
                ContentPart(
                    type="text",
                    data=system_text,
                    encoding="plain",
                )
            ],
        ),
        RuntimeMessage(
            role="user",
            parts=[
                ContentPart(
                    type="text",
                    data=user_text,
                    encoding="plain",
                )
            ],
        ),
    ]


def extract_last_user_text(messages: list[RuntimeMessage]) -> str:
    for msg in reversed(messages):
        if msg.role != "user":
            continue

        for part in msg.parts:
            if part.type == "text":
                return part.data

    return ""

def extract_last_assistant_text(messages: list[RuntimeMessage]) -> str:
    for msg in reversed(messages):
        if msg.role != "assistant":
            continue

        for part in msg.parts:
            if part.type == "text":
                return part.data

    return ""