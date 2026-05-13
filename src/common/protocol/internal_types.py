from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from common.protocol.unified_types import RuntimeMessage


class InferenceObject(BaseModel):
    user_id: str
    session_id: str | None = None
    channel: str = "api"
    stream: bool = False
    content: list[RuntimeMessage]
    metadata: dict[str, Any]

    def get_latest_message(self):
        runtime_message = self.content[0] if self.content else None
        if not runtime_message:
            return None
        message = runtime_message.parts[0] if runtime_message.parts else None

        if not message:
            return None

        if message.type == "text":
            return message
        else:
            return None
