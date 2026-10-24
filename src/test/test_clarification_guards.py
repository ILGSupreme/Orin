import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from common.protocol.agent_types import decision_for_evidence
from cortex.cortex.actions import ActionRegistry
from test_agent_support import RuntimeFixture
from test_cortex_agent import request, task


class DecisionActionTests(unittest.TestCase):
    def test_schema_and_validation_exclude_invented_actions(self):
        model = decision_for_evidence([], ["list_nodes"])
        self.assertEqual(set(model.model_json_schema()["properties"]["action"]["enum"]),
                         {"", "list_nodes"})
        with self.assertRaises(ValidationError):
            model.model_validate({"kind": "execute", "action": "inspect_node"})
        model.model_validate({"kind": "ask_user", "text": "Which node?"})

    def test_work_actions_follow_backend_availability_and_privacy(self):
        inference = SimpleNamespace(available_for=lambda role, privacy: privacy == "external_allowed")
        registry = ActionRegistry(inference=inference, commands=object())
        self.assertNotIn("describe", registry.available("local_only"))
        self.assertIn("list_nodes", registry.available("local_only"))
        self.assertIn("describe", registry.available("external_allowed"))


class ClarificationTests(RuntimeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_clarification_waits_without_tools_then_resumes_without_duplicate(self):
        runtime = self.make_runtime({"tasks": [task(clarification="Which node?")]})
        first = await runtime.handle_turn(request("Ask which node before inspecting"))
        await self.drain(runtime)
        original = await runtime.store.get(first.goal_id)
        self.assertEqual(original.status, "needs_input")
        self.assertIn("Which node?", first.text)
        self.assertEqual(runtime.actions.calls, [])
        runtime.inference.plan = {
            "tasks": [task()],
            "updates": [{"goal_id": first.goal_id, "action": "resume",
                         "task_id": "t1", "feedback": "Node name: orin-gw"}],
        }
        await runtime.handle_turn(request("The node is orin-gw"))
        await self.drain(runtime)
        self.assertEqual(len(await runtime.store.list(user_id="u", session_id="s")), 1)
        self.assertEqual((await runtime.store.get(first.goal_id)).status, "completed")

    async def test_resume_preserves_independent_new_work(self):
        runtime = self.make_runtime({"tasks": [task(clarification="Which node?")]})
        first = await runtime.handle_turn(request())
        runtime.inference.plan = {
            "tasks": [task(goal="Inspect workloads")],
            "updates": [{"goal_id": first.goal_id, "action": "resume",
                         "task_id": "t1", "feedback": "Node name: orin-gw"}],
        }
        second = await runtime.handle_turn(request("orin-gw; also inspect workloads"))
        await self.drain(runtime)
        self.assertNotEqual(first.goal_id, second.goal_id)
        self.assertEqual(len(await runtime.store.list(user_id="u", session_id="s")), 2)
