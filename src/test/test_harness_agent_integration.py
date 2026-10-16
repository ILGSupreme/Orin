"""Harness regressions without loading GPU engines or contacting a cluster."""
import asyncio
import importlib
import json
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from common.jobs import JobManager, JobSpec, PipelineStage
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import ContentPart, RuntimeMessage


def engine_double(name, **attributes):
    module = ModuleType(name)
    module.__dict__.update(attributes)
    return module


class HarnessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Replace only heavyweight inference/hardware boundaries. Harness,
        # command router, Primer facade, protocols, and JobManager are real.
        doubles = {
            "common.engine.gguf": engine_double("common.engine.gguf", GGUFPrimerEngine=object),
            "common.engine.vllm": engine_double("common.engine.vllm", VLLMPrimerEngine=object),
            "common.system.profiler": engine_double("common.system.profiler"),
        }
        with patch.dict(sys.modules, doubles):
            cls.harness_module = importlib.import_module("cortex.cortex.harness")
            cls.commands_module = importlib.import_module("cortex.cli.command_router")

    def request(self):
        return {"user_id": "u", "session_id": "s", "metadata": {}, "content": [
            RuntimeMessage(role="user", parts=[ContentPart(type="text", data="Investigate readiness")]).model_dump(),
        ]}

    def primer(self):
        return SimpleNamespace(_get_total_token_estimation=lambda **kw: 100)

    async def test_executed_batch_reaches_final_synthesis(self):
        module = self.harness_module
        observed = RuntimeMessage(role="assistant", parts=[ContentPart(type="text", data="UNIQUE_OBSERVED_RESULT")])
        result = WorkResult(work_id="task-a", status="completed", content=[observed])
        captured = []
        async def send(packet):
            captured.append(packet)
            return WorkResult(work_id=packet.work_id, status="completed", content=[observed])
        router = SimpleNamespace(retrieve_many=AsyncMock(return_value="retrieve-batch"), send=send)
        manager = JobManager()
        manager.batch_progress = AsyncMock(return_value=[result.model_dump(mode="json")])
        manager.define_pipeline("regression", stages=[
            PipelineStage(name="read_and_send_packets", runner=lambda job: module.execute_read_and_send_packets(job, router, manager)),
            PipelineStage(name="final", runner=lambda job: module.execute_final_response(job, router, self.primer())),
        ])
        with patch.object(module.memory, "get_prompts_context", AsyncMock(return_value=None)):
            job = manager.start_pipeline(spec=JobSpec(kind="test", payload={
                "inference_object": self.request(),
                "response": WorkResult(work_id="batch", status="completed", metadata={"batch_id": "send-batch"}).model_dump(),
            }), pipeline="regression")
            await asyncio.wait_for(manager.tasks[job.job_id], 1)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.spec.payload["collated_results"]["summary"]["completed"], 1)
        self.assertIn("UNIQUE_OBSERVED_RESULT", json.dumps([m.model_dump() for m in captured[0].task.messages]))

    async def test_task_packet_ids_are_unique(self):
        shaped = {"task_type": "llm", "task_role": "llm", "task_operation": "analyze", "task_message": "inspect"}
        first = await self.harness_module._build_work_packet("u", shaped, self.primer())
        second = await self.harness_module._build_work_packet("u", shaped, self.primer())
        self.assertNotEqual(first.work_id, second.work_id)

    async def test_agent_actions_are_explicit_and_ignore_selected_backend(self):
        module = self.commands_module
        calls = []
        async def inspect(ctx, args):
            calls.append((ctx.backend, args))
            return "ready"
        router = module.CommandRouter.__new__(module.CommandRouter)
        router.root_folder = module.CommandFolder(name="Root", desc="test", commands=[module.Command(
            command="/list_nodes", desc="inspect", kind="text", handler=inspect,
            modes={"terminal"}, harness_action="list_nodes", safe_info_action=True,
        )])
        router.shell_state = module.ShellState(selected_backend_name="unrelated-backend")
        self.assertEqual(await router.execute_agent_action("list_nodes"), "ready")
        self.assertEqual(calls, [(None, "")])
        with self.assertRaises(ValueError):
            await router.execute_agent_action("delete_pod")
        router.root_folder.commands[0].suggest_only = True
        with self.assertRaises(ValueError):
            await router.execute_agent_action("list_nodes")

    async def test_normal_chat_does_not_start_pipeline(self):
        from cortex.cortex.harness_types import ChatIngressInterpretationModel
        from common.protocol.internal_types import InferenceObject
        manager = JobManager()
        harness = self.harness_module.Harness(self.primer(), SimpleNamespace(), manager, SimpleNamespace())
        harness.collate_system_information = AsyncMock(return_value="")
        harness._lightweight_interpret = AsyncMock(return_value=ChatIngressInterpretationModel(mode="chat", action="answer_only"))
        plan = await harness._response_chat(InferenceObject.model_validate(self.request()), "chat")
        self.assertIsNone(plan.background_job)
        self.assertEqual(manager.list_jobs(), [])

    async def test_direct_work_ingress_cannot_start_agent_actions(self):
        from common.protocol.routing_types import CanonicalTask, WorkPacket, WorkType
        manager = JobManager()
        harness = self.harness_module.Harness(self.primer(), SimpleNamespace(), manager, SimpleNamespace())
        result = await harness.handle_work_packet(WorkPacket(
            work_id="external", task=CanonicalTask(work_type=WorkType.TOOL, operation="list_nodes"),
            metadata={"pipeline": "agent", "admin": True},
        ))
        self.assertEqual(result.status, "failed")
        self.assertEqual(manager.list_jobs(), [])

    async def test_agent_pipeline_runs_through_harness(self):
        from cortex.cortex.harness_types import ChatIngressInterpretationModel
        from common.protocol.internal_types import InferenceObject
        from test_cortex_agent import FakePrimer, FakeCommands
        manager = JobManager()
        primer = FakePrimer([
            {"decision": "execute_action", "action": "list_nodes"},
            {"decision": "finish", "satisfied": True, "answer": "node-a ready", "evidence": [1]},
        ])
        harness = self.harness_module.Harness(primer, SimpleNamespace(), manager, FakeCommands())
        harness.collate_system_information = AsyncMock(return_value="")
        harness._lightweight_interpret = AsyncMock(return_value=ChatIngressInterpretationModel(
            mode="chat", action="start_pipeline", pipeline="agent",
        ))
        plan = await harness._response_chat(InferenceObject.model_validate(self.request()), "chat")
        job = plan.background_job
        self.assertIsNotNone(job)
        await asyncio.wait_for(manager.tasks[job.job_id], 1)
        self.assertEqual(job.spec.kind, "harness.agent")
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.get_result()["metadata"]["agent_state"]["outcome"], "completed")

    async def test_completed_job_summary_does_not_dump_payload(self):
        manager = JobManager()
        job = manager._create_job(JobSpec(kind="harness.agent", payload={
            "session_id": "s", "agent_state": {"observations": ["INTERNAL_PAYLOAD_MARKER"]},
        }))
        job.status = "completed"
        job.latest_result = WorkResult(work_id=job.job_id, status="completed", content=[
            RuntimeMessage(role="assistant", parts=[ContentPart(type="text", data="visible result")]),
        ]).model_dump()
        summary = manager.summarize_jobs_for_prompt("s")
        self.assertIn("visible result", summary)
        self.assertNotIn("INTERNAL_PAYLOAD_MARKER", summary)

    async def test_job_result_is_retrievable_through_harness(self):
        from cortex.cortex.harness_types import ChatIngressInterpretationModel
        from common.protocol.internal_types import InferenceObject
        manager = JobManager()
        harness = self.harness_module.Harness(self.primer(), SimpleNamespace(), manager, SimpleNamespace())
        previous = manager._create_job(JobSpec(kind="harness.chat", payload={"session_id": "s"}))
        previous.status = "completed"
        previous.latest_result = WorkResult(status="completed", work_id=previous.job_id, content=[
            RuntimeMessage(role="assistant", parts=[ContentPart(type="text", data="Completed finding")]),
        ]).model_dump()
        harness.collate_system_information = AsyncMock(return_value="")
        harness._lightweight_interpret = AsyncMock(return_value=ChatIngressInterpretationModel(mode="chat", action="get_job_result"))
        plan = await harness._response_chat(InferenceObject.model_validate(self.request()), "chat")
        self.assertTrue(plan.respond_directly)
        self.assertIn("Completed finding", plan.direct_text)
