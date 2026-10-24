"""Deterministic model/tool boundaries backed by the real Memory handlers and SQL."""

import inspect
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from common.protocol.routing_types import WorkPacket
from cortex.cortex.actions import ActionResult
from cortex.cortex.agent import AgentRuntime
from cortex.cortex.goal_store import GoalStore
from cortex.cortex.inference import InferenceResult
from common.jobs import JobManager
from memory import sql
from memory.runtime import MemoryRuntime


def payload(messages):
    return json.loads(messages[-1].parts[0].data)


class MemoryHTTP:
    def __init__(self, path):
        self.runtime = MemoryRuntime.__new__(MemoryRuntime)
        self.runtime._memoryDB = sql.MemoryDB(path)

    async def post(self, url, *, json, timeout):
        result = await self.runtime.handle_ingress(WorkPacket.model_validate(json))
        return SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: result.model_dump(mode="json")
        )


class FakeInference:
    def __init__(self, plan=None, decide=None):
        self.plan, self.decide, self.calls = plan, decide, []

    async def generate(self, role, messages, *, model=None, charge=None, **kwargs):
        data = payload(messages)
        self.calls.append((role, data, kwargs))
        if charge:
            await charge(100)
        result = (
            self.plan(data)
            if role == "interpret" and callable(self.plan)
            else self.plan
        )
        if role != "interpret":
            result = self.decide(data)
        if inspect.isawaitable(result):
            result = await result
        return InferenceResult(text=json.dumps(result), tokens=100, backend="fake")


class FakeActions:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler

    def available(self, privacy="local_only"):
        return {
            "news_search": "Retrieve current news",
            "list_nodes": "Inspect nodes",
            "summarize": "Summarize input",
        }

    async def execute(self, name, text, **kwargs):
        self.calls.append((name, text, kwargs))
        await kwargs["checkpoint"]("work:" + str(len(self.calls)))
        result = (
            self.handler(name, text, kwargs)
            if self.handler
            else "real tool observation"
        )
        if inspect.isawaitable(result):
            result = await result
        return ActionResult(text=result)


def auto_decide(data):
    current = [
        o
        for o in data["observations"]
        if o["status"] == "completed" and o["cycle"] == data["cycle"]
    ]
    if current:
        return {
            "kind": "finish",
            "text": "Verified: " + current[-1]["text"],
            "satisfied": True,
            "evidence": [current[-1]["id"]],
        }
    return {"kind": "execute", "action": "list_nodes", "text": ""}


class RuntimeFixture:
    def make_runtime(self, plan=None, decide=auto_decide, actions=None, limits=None):
        from common.protocol.agent_types import AgentLimits

        if not hasattr(self, "db_path"):
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            self.db_path = Path(temp.name) / "memory.db"
            for name in ("NOTES_DIR", "SUMMARIES_DIR", "DOCS_DIR", "YAML_DIR"):
                patcher = patch.object(sql, name, Path(temp.name) / name)
                patcher.start()
                self.addCleanup(patcher.stop)
        store = GoalStore(
            http=MemoryHTTP(self.db_path), base_url="http://memory", namespace="lab"
        )
        jobs = JobManager()
        self.addAsyncCleanup(jobs.shutdown)
        runtime = AgentRuntime(
            inference=FakeInference(plan=plan, decide=decide),
            actions=actions or FakeActions(),
            store=store,
            jobs=jobs,
            limits=limits or AgentLimits(),
        )
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def drain(self, runtime):
        import asyncio

        while runtime.jobs.tasks:
            await asyncio.wait_for(
                asyncio.gather(
                    *list(runtime.jobs.tasks.values()), return_exceptions=True
                ),
                2,
            )


def memory_router(http):
    async def send(packet):
        return await http.runtime.handle_ingress(packet)

    return SimpleNamespace(send=send)
