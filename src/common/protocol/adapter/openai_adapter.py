from typing import Any, Tuple, get_args

from common.protocol.adapter.message_adapter import (
    _THINK_SPLIT_RE,
    NO_THINK_INSTRUCTION,
)
from common.protocol.unified_types import ContentPart, MessageRole, RuntimeMessage


class OpenAIStyleMessageAdapter:
    nothink: bool

    def __init__(self, *, nothink: bool = False):
        self.nothink = nothink

    def render_messages(
        self,
        *,
        messages: list[RuntimeMessage],
        operation: str | None = None,
        constraints: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        rendered: list[dict[str, Any]] = []

        for msg in messages:
            content_blocks: list[dict[str, Any]] = []
            text_chunks: list[str] = []

            for part in msg.parts:
                if part.type == "text":
                    text_chunks.append(part.data)

                elif part.type == "image":
                    content_blocks.append(
                        {
                            "type": "image",
                            "image": part.data,
                        }
                    )

                elif part.type == "json":
                    text_chunks.append(part.data)

                else:
                    text_chunks.append(
                        f"[unsupported part: type={part.type}, mime_type={part.mime_type}]"
                    )

            if content_blocks:
                if text_chunks:
                    content_blocks.insert(
                        0,
                        {
                            "type": "text",
                            "text": "\n".join(text_chunks),
                        },
                    )
                rendered.append(
                    {
                        "role": msg.role,
                        "content": content_blocks,
                    }
                )
            else:
                rendered.append(
                    {
                        "role": msg.role,
                        "content": "\n".join(text_chunks),
                    }
                )

        if self.nothink:
            rendered = self._inject_no_think_instruction(rendered)

        return rendered

    def _inject_no_think_instruction(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        updated = [dict(m) for m in messages]

        for msg in updated:
            if msg.get("role") == "system":
                content = msg.get("content", "")

                if isinstance(content, str):
                    if NO_THINK_INSTRUCTION not in content:
                        msg["content"] = f"{content}\n\n{NO_THINK_INSTRUCTION}"
                    return updated

                if isinstance(content, list):
                    text_block = {
                        "type": "text",
                        "text": NO_THINK_INSTRUCTION,
                    }
                    if text_block not in content:
                        content.insert(0, text_block)
                    return updated

        return [
            {
                "role": "system",
                "content": NO_THINK_INSTRUCTION,
            },
            *updated,
        ]

    def parse_response(
        self,
        *,
        content: Any,
        role: MessageRole,
    ) -> list[RuntimeMessage]:
        if content is None:
            return []

        reasoning: str | None = None
        visible: Any = content

        if self.nothink:
            reasoning, visible = self._split_reasoning_and_visible(content)

        messages: list[RuntimeMessage] = []

        # Parse visible/user-facing content
        if visible is not None:
            if isinstance(visible, str):
                messages.append(
                    RuntimeMessage(
                        role=role,
                        parts=[
                            ContentPart(
                                type="text",
                                data=visible,
                                encoding="plain",
                            )
                        ],
                        metadata={
                            "visibility": "user",
                            "kind": "final",
                        },
                    )
                )

            elif isinstance(visible, dict):
                parsed = self._parse_message_dict(visible, default_role=role)
                parsed.metadata.setdefault("visibility", "user")
                parsed.metadata.setdefault("kind", "final")
                messages.append(parsed)

            elif isinstance(visible, list):
                if all(isinstance(item, dict) and "role" in item for item in visible):
                    for item in visible:
                        parsed = self._parse_message_dict(item, default_role=role)
                        parsed.metadata.setdefault("visibility", "user")
                        parsed.metadata.setdefault("kind", "final")
                        messages.append(parsed)
                else:
                    messages.append(
                        RuntimeMessage(
                            role=role,
                            parts=self._parse_content_blocks(visible),
                            metadata={
                                "visibility": "user",
                                "kind": "final",
                            },
                        )
                    )

            else:
                messages.append(
                    RuntimeMessage(
                        role=role,
                        parts=[
                            ContentPart(
                                type="text",
                                data=str(visible),
                                encoding="plain",
                            )
                        ],
                        metadata={
                            "visibility": "user",
                            "kind": "final",
                        },
                    )
                )

        # Parse reasoning/internal content
        if reasoning:
            messages.append(
                RuntimeMessage(
                    role=role,
                    parts=[
                        ContentPart(
                            type="text",
                            data=reasoning,
                            encoding="plain",
                        )
                    ],
                    metadata={
                        "visibility": "internal",
                        "kind": "reasoning",
                    },
                )
            )

        return messages

    def _parse_message_dict(
        self,
        message: dict[str, Any],
        *,
        default_role: MessageRole = "assistant",
    ) -> RuntimeMessage:
        role_raw = message.get("role") or default_role
        # Check if raw_role is one of the allowed values in MessageRole
        if role_raw in get_args(MessageRole):
            role_to_use = role_raw
        else:
            role_to_use = "assistant"  # Fallback

        content = message.get("content")

        if isinstance(content, str):
            parts = [
                ContentPart(
                    type="text",
                    data=content,
                    encoding="plain",
                )
            ]
        elif isinstance(content, list):
            parts = self._parse_content_blocks(content)
        elif content is None:
            parts = []
        else:
            parts = [
                ContentPart(
                    type="text",
                    data=str(content),
                    encoding="plain",
                )
            ]

        return RuntimeMessage(
            role=role_to_use,
            parts=parts,
            metadata={
                "visibility": "user",
                "kind": "final",
            },
        )

    def _parse_content_blocks(
        self,
        blocks: list[Any],
    ) -> list[ContentPart]:
        parts: list[ContentPart] = []

        for block in blocks:
            if not isinstance(block, dict):
                parts.append(
                    ContentPart(
                        type="text",
                        data=str(block),
                        encoding="plain",
                    )
                )
                continue

            block_type = block.get("type")

            if block_type == "text":
                parts.append(
                    ContentPart(
                        type="text",
                        data=str(block.get("text", "")),
                        encoding="plain",
                    )
                )
            elif block_type == "image":
                parts.append(
                    ContentPart(
                        type="image",
                        data=str(block.get("image", "")),
                        encoding="base64",
                    )
                )
            else:
                parts.append(
                    ContentPart(
                        type="text",
                        data=str(block),
                        encoding="plain",
                        metadata={"source_block_type": block_type},
                    )
                )

        return parts

    def _split_reasoning_and_visible(self, text: str) -> Tuple[str | None, str]:
        if not text:
            return None, ""

        m = _THINK_SPLIT_RE.match(text)
        if not m:
            return None, text.strip()

        reasoning = m.group(1).strip()
        visible = m.group(2).strip()
        return reasoning, visible
