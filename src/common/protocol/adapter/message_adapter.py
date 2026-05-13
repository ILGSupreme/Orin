import re
from typing import Any, Protocol

from common.protocol.unified_types import MessageRole, RuntimeMessage

_THINK_SPLIT_RE = re.compile(r"^\s*<think>(.*?)</think>\s*(.*)$", re.DOTALL)

NO_THINK_INSTRUCTION = (
    "Do not output reasoning, chain-of-thought, or <think> tags. /no_think"
)


class MessageAdapter(Protocol):
    nothink: bool

    def render_messages(
        self,
        *,
        messages: list[RuntimeMessage],
        operation: str,
        constraints: dict[str, Any],
    ) -> list[dict[str, Any]] | str: ...

    def parse_response(
        self, *, content: Any, role: MessageRole
    ) -> list[RuntimeMessage]: ...
