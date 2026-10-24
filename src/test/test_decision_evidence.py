"""Evidence contracts; inference and persistence are mocked in the step test."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import ValidationError

from common.protocol.agent_types import (
    AgentLimits, Goal, Observation, TaskSpec, TaskState, decision_for_evidence,
)
from common.protocol.grammar_schema import grammar_schema
from cortex.cortex.agent import AgentRuntime
from cortex.cortex.inference import InferenceResult, strict_schema


class EvidenceSchemaTests(unittest.TestCase):
    def test_exact_ids_pass_but_prose_and_invented_ids_fail(self):
        model = decision_for_evidence(["obs-1", "dependency:t1"])
        for evidence in (["obs-1"], ["dependency:t1"], []):
            model(kind="finish", evidence=evidence)
        for value in ("invented", "Observation id: obs-1, action: list_nodes"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                model(kind="finish", evidence=[value])
        schema = grammar_schema(strict_schema(model))
        self.assertEqual(
            set(schema["properties"]["evidence"]["items"]["enum"]),
            {"obs-1", "dependency:t1"},
        )

    def test_no_evidence_remains_empty_after_grammar_projection(self):
        model = decision_for_evidence([])
        model(kind="execute", evidence=[])
        with self.assertRaises(ValidationError):
            model(kind="finish", evidence=["invented"])
        schema = grammar_schema(strict_schema(model))
        self.assertEqual(schema["properties"]["evidence"]["const"], [])

    def test_models_do_not_share_evidence_across_decisions(self):
        first = decision_for_evidence(["first"])
        second = decision_for_evidence(["second"])
        first(kind="finish", evidence=["first"])
        second(kind="finish", evidence=["second"])
        with self.assertRaises(ValidationError):
            second(kind="finish", evidence=["first"])


class EvidenceStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_excludes_failed_old_cycle_and_unrelated_evidence(self):
        dependency = TaskState(
            spec=TaskSpec(id="dep", goal="Inspect", criteria="Observed"),
            status="completed", answer="Dependency observation",
        )
        unrelated = TaskState(
            spec=TaskSpec(id="other", goal="Other", criteria="Observed"),
            status="completed", answer="Unrelated result",
        )
        task = TaskState(
            spec=TaskSpec(id="t1", goal="Report", criteria="Verified", depends_on=["dep"]),
            cycles=1,
            observations=[
                Observation(id="current", action="list_nodes", status="completed", text="ready", cycle=1),
                Observation(id="old", action="list_nodes", status="completed", text="ready", cycle=0),
                Observation(id="failed", action="list_nodes", status="failed", text="error", cycle=1),
                Observation(id="feedback", action="user_feedback", status="user", text="ready", cycle=1),
            ],
        )
        goal = Goal(user_id="u", session_id="s", tasks=[dependency, unrelated, task])

        async def generate(role, messages, *, model, **kwargs):
            self.assertEqual(role, "decide")
            payload = json.loads(messages[-1].parts[0].data)
            self.assertEqual(payload["eligible_evidence_ids"], ["current", "dependency:dep"])
            for invalid in ("old", "failed", "feedback", "dependency:other"):
                with self.assertRaises(ValidationError):
                    model(kind="finish", evidence=[invalid])
            result = model(kind="finish", text="Observed ready", satisfied=True,
                           evidence=["current", "dependency:dep"])
            return InferenceResult(text=result.model_dump_json(), tokens=10, backend="fake")

        runtime = AgentRuntime(
            inference=SimpleNamespace(generate=generate),
            actions=SimpleNamespace(available=lambda privacy="local_only": {"list_nodes": "Inspect"}),
            store=None, jobs=None, limits=AgentLimits(),
        )
        runtime._save = AsyncMock()
        await runtime._step(SimpleNamespace(), goal, task)
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.answer, "Observed ready")
