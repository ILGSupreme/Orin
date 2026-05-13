import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field


@dataclass
class LogStream:
    max_lines: int = 500
    history: deque[str] = field(default_factory=lambda: deque(maxlen=500))
    listeners: set[asyncio.Queue[str]] = field(default_factory=set)

    def publish(self, line: str) -> None:
        if not line.endswith("\n"):
            line += "\n"

        self.history.append(line)

        for queue in list(self.listeners):
            try:
                queue.put_nowait(line)
            except asyncio.QueueFull:
                pass

    async def stream(self, *, tail: int = 100):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self.listeners.add(queue)

        try:
            for line in list(self.history)[-tail:]:
                yield line

            while True:
                line = await queue.get()
                yield line

        except asyncio.CancelledError:
            raise

        finally:
            self.listeners.discard(queue)


class LogStreamHandler(logging.Handler):
    def __init__(self, stream: LogStream):
        super().__init__()
        self.stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.stream.publish(self.format(record))
        except Exception:
            self.handleError(record)
