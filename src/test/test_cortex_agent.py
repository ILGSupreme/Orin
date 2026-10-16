"""Exercise the real agent loop with deterministic model and transport doubles."""
import asyncio
import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from common.jobs import Job, JobManager, JobSpec, PipelineStage
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import ContentPart, RuntimeMessage
from cortex.cortex.agent import AgentRunner
from cortex.cortex.agent_types import AgentLimits, DECISION_ADAPTER
from cortex.cortex.harness_types import ChatIngressInterpretationModel, TerminalIngressInterpretationModel


def message(text):
    return RuntimeMessage(role="user", parts=[ContentPart(type="text", data=text)])


def make_job():
    return Job(job_id="test-agent", spec=JobSpec(kind="harness.agent", payload={
        "inference_object": {"user_id": "u", "session_id": "s", "metadata": {},
                             "content": [message("Inspect the cluster and investigate its nodes").model_dump()]},
    }))


class FakePrimer:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.contexts = []

    async def chat_json(self, *, messages, **kwargs):
        context = json.loads(messages[-1].parts[0].data)
        self.contexts.append(context)
        choice = next(self.decisions)
        return choice(context) if callable(choice) else choice

    def _get_total_token_estimation(self, **kwargs):
        return 100

    def _can_fit_request(self, **kwargs):
        return True

    def status(self):
        return {"effective_n_ctx": 1792}


class FakeCommands:
    def __init__(self):
        self.calls = []

    async def execute_agent_action(self, action):
        self.calls.append(action)
        return {"cluster_snapshot": "Backend degraded; inspect nodes", "list_nodes": "node-a ready"}[action]


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def runner(self, decisions, **limits):
        self.primer = FakePrimer(decisions)
        self.commands = FakeCommands()
        return AgentRunner(self.primer, SimpleNamespace(), self.commands, AgentLimits(**limits))

    async def test_next_action_depends_on_observation(self):
        def follow_up(context):
            self.assertIn("degraded", context["observations"][0]["text"])
            return {"decision": "execute_action", "action": "list_nodes"}
        runner = self.runner([
            {"decision": "execute_action", "action": "cluster_snapshot"}, follow_up,
            {"decision": "finish", "satisfied": True, "answer": "node-a ready", "evidence": [1, 2]},
        ])
        result = await runner.run(make_job())
        self.assertEqual(self.commands.calls, ["cluster_snapshot", "list_nodes"])
        state = result.metadata["agent_state"]
        self.assertEqual(state["outcome"], "completed")
        self.assertNotEqual(state["observations"][0]["work_id"], state["observations"][1]["work_id"])

    async def test_invalid_or_unavailable_action_never_executes(self):
        runner = self.runner([{"decision": "execute_action", "action": "deploy_pod"}])
        result = await runner.run(make_job())
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.commands.calls, [])

    async def test_failed_action_can_lead_to_partial_result(self):
        runner = self.runner([
            {"decision": "execute_action", "action": "list_pods"},
            {"decision": "finish", "satisfied": False, "answer": "Inspection unavailable"},
        ])
        result = await runner.run(make_job())
        self.assertEqual(result.metadata["agent_state"]["outcome"], "partial")
        self.assertEqual(result.metadata["agent_state"]["observations"][0]["status"], "failed")

    async def test_finish_requires_successful_evidence(self):
        runner = self.runner([{"decision": "finish", "satisfied": True, "answer": "done", "evidence": [9]}])
        self.assertEqual((await runner.run(make_job())).status, "failed")

    async def test_repeated_action_is_bounded(self):
        action = {"decision": "execute_action", "action": "list_nodes"}
        runner = self.runner([action, action])
        result = await runner.run(make_job())
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.commands.calls, ["list_nodes"])

    async def test_step_budget_returns_partial_observations(self):
        runner = self.runner([{"decision": "execute_action", "action": "list_nodes"}], max_steps=1)
        result = await runner.run(make_job())
        self.assertEqual(result.metadata["agent_state"]["outcome"], "exhausted")
        self.assertEqual(len(result.metadata["agent_state"]["observations"]), 1)

    async def test_token_budget_prevents_model_call(self):
        runner = self.runner([], token_budget=1)
        result = await runner.run(make_job())
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.primer.contexts, [])

    async def test_context_limit_prevents_model_call(self):
        runner = self.runner([])
        self.primer._can_fit_request = lambda **kwargs: False
        result = await runner.run(make_job())
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.primer.contexts, [])

    async def test_timeout_interrupts_decision(self):
        runner = self.runner([], timeout_seconds=0.01)
        async def hang(**kwargs):
            await asyncio.Event().wait()
        self.primer.chat_json = hang
        result = await runner.run(make_job())
        self.assertEqual(result.metadata["agent_state"]["outcome"], "exhausted")

    async def test_ask_user_stops_without_actions(self):
        runner = self.runner([{"decision": "ask_user", "question": "Which workload?"}])
        result = await runner.run(make_job())
        self.assertEqual(result.metadata["agent_state"]["outcome"], "awaiting_user")
        self.assertEqual(self.commands.calls, [])

    async def test_routed_action_polls_and_provides_observation(self):
        runner = self.runner([
            {"decision": "execute_action", "action": "analyze", "text": "Analyze supplied data"},
            {"decision": "finish", "satisfied": True, "answer": "Analysis complete", "evidence": [1]},
        ])
        calls = []
        # Routed work can require a larger context than the local decision model.
        self.primer._can_fit_request = lambda **kwargs: kwargs["reserved_output_tokens"] == 256
        async def send(packet):
            calls.append(packet)
            return WorkResult(work_id="backend-1", status="accepted", metadata={"backend_ref": {"name": "llm"}})
        async def retrieve(*args):
            return WorkResult(work_id="backend-1", status="completed", content=[
                RuntimeMessage(role="assistant", parts=[ContentPart(type="text", data="Analysis complete")]),
            ])
        runner.router = SimpleNamespace(send=send, retrieve_completed_work=retrieve)
        result = await runner.run(make_job())
        self.assertEqual(result.status, "completed")
        self.assertEqual(calls[0].operation, "analyze")
        self.assertEqual(self.primer.contexts[1]["observations"][0]["text"], "Analysis complete")

    def test_decision_arguments_are_strict(self):
        with self.assertRaises(ValidationError):
            DECISION_ADAPTER.validate_python({"decision": "execute_action", "action": "list_nodes", "admin": True})

    def test_result_retrieval_and_agent_pipeline_validate(self):
        self.assertEqual(ChatIngressInterpretationModel(mode="chat", action="get_job_result").action, "get_job_result")
        TerminalIngressInterpretationModel(mode="terminal", intent="job_status", action="get_job_result")
        self.assertEqual(ChatIngressInterpretationModel(mode="chat", action="start_pipeline", pipeline="agent").pipeline, "agent")
        self.assertIsNone(ChatIngressInterpretationModel(mode="chat", action="answer_only", pipeline="agent").pipeline)


class JobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_timeout_covers_runner_and_preserves_failed_workresult(self):
        manager = JobManager()
        async def hang(job):
            await asyncio.Event().wait()
        manager.define_pipeline("hang", stages=[PipelineStage(name="hang", runner=hang, timeout_seconds=0.01)])
        job = manager.start_pipeline(spec=JobSpec(kind="test"), pipeline="hang")
        with self.assertLogs(level="ERROR"):
            await asyncio.wait_for(manager.tasks[job.job_id], 1)
        self.assertEqual(job.status, "failed")
        self.assertEqual(WorkResult.model_validate(job.get_result()).status, "failed")

    async def test_stage_timeout_covers_hung_poller(self):
        manager = JobManager()
        async def start(job):
            return WorkResult(status="accepted", work_id=job.job_id)
        async def hang(job, result):
            await asyncio.Event().wait()
        manager.define_pipeline("poll", stages=[PipelineStage(name="poll", runner=start, poller=hang,
                                                             timeout_seconds=0.02, poll_interval_seconds=0.001)])
        job = manager.start_pipeline(spec=JobSpec(kind="test"), pipeline="poll")
        with self.assertLogs(level="ERROR"):
            await asyncio.wait_for(manager.tasks[job.job_id], 1)
        self.assertEqual(job.status, "failed")

    async def test_cancellation_stops_agent_and_updates_state(self):
        manager = JobManager()
        entered = asyncio.Event()
        primer = FakePrimer([])
        async def hang(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        primer.chat_json = hang
        runner = AgentRunner(primer, SimpleNamespace(), FakeCommands())
        job = manager.start(spec=make_job().spec, runner=runner.run)
        task = manager.tasks[job.job_id]
        await entered.wait()
        self.assertTrue(manager.cancel(job.job_id))
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.spec.payload["agent_state"]["outcome"], "cancelled")
        self.assertTrue(job.get_result()["metadata"]["cancelled"])

    async def test_cancel_queued_job_and_shutdown(self):
        manager = JobManager(max_concurrent_jobs=1)
        async def hang(job):
            await asyncio.Event().wait()
        first = manager.start(spec=JobSpec(kind="test"), runner=hang)
        second = manager.start(spec=JobSpec(kind="test"), runner=hang)
        self.assertTrue(manager.cancel(second.job_id))
        await manager.shutdown()
        self.assertEqual(first.status, "failed")
        self.assertEqual(second.status, "failed")
        self.assertFalse(manager.tasks)
