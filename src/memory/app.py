from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from common.log import LogStream, LogStreamHandler
from common.protocol.routing_types import WorkPacket
from memory.runtime import MemoryRuntime

log_stream = LogStream()
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

has_stdout_handler = any(
    isinstance(h, logging.StreamHandler) for h in root_logger.handlers
)

if not has_stdout_handler:
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
    )
    root_logger.addHandler(stdout_handler)

attach_handler = LogStreamHandler(log_stream)
attach_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(name)s | %(levelname)s | %(message)s")
)
root_logger.addHandler(attach_handler)

memory_runtime = MemoryRuntime()
app = FastAPI(title="memory")

# ---------------------------------------------------------------------------
# Basic service endpoints
# ---------------------------------------------------------------------------

@app.post("/work")
async def submit_work(req: WorkPacket):
    return await memory_runtime.handle_ingress(req)


@app.get("/work/{work_id}")
async def fetch_work(work_id: str):
    return await memory_runtime.handle_egress(work_id)


@app.get("/attach")
async def attach(tail: int = 100):
    return StreamingResponse(
        log_stream.stream(tail=tail),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/live")
async def live():
    return {"ok": True}


@app.get("/ready")
async def ready():
    return {
        "ok": True,
        "accepting_requests": True,
    }


@app.get("/health")
async def health():
    return {"ok": True}
