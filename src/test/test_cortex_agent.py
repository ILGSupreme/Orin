import asyncio
import json
import time
import unittest

from pydantic import ValidationError

from common.jobs import JobManager, JobSpec, PipelineStage
from common.protocol.agent_types import AgentLimits, Goal, TaskSpec, TaskState, TurnPlan
from common.protocol.internal_types import InferenceObject
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import ContentPart, RuntimeMessage
from test_agent_support import FakeActions, RuntimeFixture, auto_decide


def request(text="Hi, explain this and get news", **metadata):
    return InferenceObject(
        user_id="u",
        session_id="s",
        metadata=metadata,
        content=[
            RuntimeMessage(role="user", parts=[ContentPart(type="text", data=text)])
        ],
    )


def task(id="t1", goal="Inspect nodes", **extra):
    return {
        "id": id,
        "goal": goal,
        "criteria": "Evidence establishes the result",
        **extra,
    }


class UnifiedRuntimeTests(RuntimeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_retry_with_same_turn_id_does_not_duplicate_delegated_work(self):
        runtime = self.make_runtime({"tasks": [task(event_key="new-data")]})
        first = await runtime.handle_turn(request(turn_id="turn-1"))
        second = await runtime.handle_turn(request(turn_id="turn-1"))
        self.assertEqual(first.goal_id, second.goal_id)
        self.assertEqual(len(runtime.inference.calls), 1)
        self.assertEqual(len(await runtime.store.list(user_id="u", session_id="s")), 1)

    async def test_private_goals_are_excluded_from_later_external_turn_context(self):
        runtime = self.make_runtime(
            {"tasks": [task(goal="PRIVATE_GOAL", event_key="event")]}
        )
        await runtime.handle_turn(request())
        runtime.inference.plan = {"reply": "Hello", "tasks": []}
        await runtime.handle_turn(request("Hello", privacy="external_allowed"))
        self.assertNotIn("PRIVATE_GOAL", json.dumps(runtime.inference.calls[-1]))

    async def test_private_result_keeps_reply_private_even_when_requested_in_external_turn(
        self,
    ):
        runtime = self.make_runtime({"tasks": [task()]})
        first = await runtime.handle_turn(request())
        await self.drain(runtime)
        runtime.inference.plan = {
            "updates": [{"goal_id": first.goal_id, "action": "result"}]
        }
        result = await runtime.handle_turn(
            request("Show result", privacy="external_allowed")
        )
        self.assertEqual(result.privacy, "local_only")

    async def test_only_selected_input_parts_reach_a_task_action(self):
        runtime = self.make_runtime({"tasks": [task(input_refs=["0:1"])]})
        req = request("Explain the attached document")
        req.content[0].parts.extend(
            [
                ContentPart(type="text", data="SELECTED_DOCUMENT"),
                ContentPart(type="text", data="UNRELATED_DOCUMENT"),
            ]
        )
        await runtime.handle_turn(req)
        await self.drain(runtime)
        wire = json.dumps(
            [
                message.model_dump()
                for message in runtime.actions.calls[0][2]["messages"]
            ]
        )
        self.assertIn("SELECTED_DOCUMENT", wire)
        self.assertNotIn("UNRELATED_DOCUMENT", wire)

    async def test_simple_conversation_finishes_without_background_work(self):
        runtime = self.make_runtime({"reply": "Hello!", "tasks": []})
        turn = await runtime.handle_turn(request("Hello"))
        self.assertEqual(turn.text, "Hello!")
        self.assertIsNone(turn.goal_id)
        self.assertEqual(runtime.jobs.list_jobs(), [])
        self.assertEqual(await runtime.store.list(user_id="u", session_id="s"), [])

    async def test_task_reply_does_not_repeat_confirmation_or_claim_completion(self):
        for text in ("Would you like me to inspect the nodes now?",
                     "All nodes are ready; inspection is complete."):
            with self.subTest(text=text):
                runtime = self.make_runtime({"reply": text, "tasks": [task()]})
                turn = await runtime.handle_turn(request("Yes, inspect them now"))
                self.assertNotIn(text, turn.text)
                self.assertIn("working", turn.text)
                await self.drain(runtime)
                goal = await runtime.store.get(turn.goal_id)
                self.assertEqual(goal.reply, "")
                self.assertEqual(goal.status, "completed")
                self.assertEqual([c[0] for c in runtime.actions.calls], ["list_nodes"])

    async def test_scheduled_task_retry_does_not_replay_discarded_reply(self):
        runtime = self.make_runtime({
            "reply": "Should I monitor those nodes?", "tasks": [task(event_key="new-data")]
        })
        first = await runtime.handle_turn(request(turn_id="scheduled"))
        # Existing persisted goals may predate reply scoping.
        stored = await runtime.store.get(first.goal_id)
        stored.reply = "Should I monitor those nodes?"
        await runtime.store.save(stored)
        second = await runtime.handle_turn(request(turn_id="scheduled"))
        self.assertNotIn("Should I", first.text)
        self.assertNotIn("Should I", second.text)
        self.assertEqual(first.goal_id, second.goal_id)
        self.assertEqual(len(runtime.inference.calls), 1)

    async def test_resume_update_uses_runtime_acknowledgement(self):
        runtime = self.make_runtime(
            {"tasks": [task()]},
            lambda data: {"kind": "ask_user", "text": "Which nodes?"},
        )
        first = await runtime.handle_turn(request())
        await self.drain(runtime)
        runtime.inference.decide = auto_decide
        runtime.inference.plan = {
            "reply": "Would you like me to inspect them now?",
            "updates": [{"goal_id": first.goal_id, "action": "resume",
                         "feedback": "Inspect all nodes"}],
        }
        turn = await runtime.handle_turn(request("All nodes, please"))
        self.assertNotIn("Would you like", turn.text)
        self.assertIn("Updated", turn.text)
        await self.drain(runtime)
        self.assertEqual((await runtime.store.get(first.goal_id)).status, "completed")

    async def test_result_update_does_not_echo_invented_interpreter_result(self):
        runtime = self.make_runtime({"tasks": [task()]})
        first = await runtime.handle_turn(request())
        await self.drain(runtime)
        runtime.inference.plan = {
            "reply": "All nodes failed!",
            "updates": [{"goal_id": first.goal_id, "action": "result"}],
        }
        turn = await runtime.handle_turn(request("What was the result?"))
        goal = await runtime.store.get(first.goal_id)
        self.assertEqual(turn.text, goal.result)
        self.assertNotIn("All nodes failed", turn.text)

    async def test_independent_answer_survives_status_update(self):
        runtime = self.make_runtime({"tasks": [task(event_key="new-data")]})
        first = await runtime.handle_turn(request())
        runtime.inference.plan = {
            "reply": "Kubernetes orchestrates containers.",
            "reply_scope": "independent_answer",
            "updates": [{"goal_id": first.goal_id, "action": "status"}],
        }
        turn = await runtime.handle_turn(request("What is Kubernetes and what is my task status?"))
        self.assertIn("Kubernetes orchestrates", turn.text)
        self.assertIn(first.goal_id, turn.text)

    async def test_mixed_turn_preserves_immediate_reply_and_independent_requests(self):
        runtime = self.make_runtime(
            {
                "reply": "Hello. This is a sensor.",
                "reply_scope": "independent_answer",
                "tasks": [
                    task("news", "Get current news", context="NEWS_ONLY"),
                    task("doc", "Summarize document", context="DOCUMENT_ONLY"),
                ],
            }
        )
        turn = await runtime.handle_turn(request())
        self.assertIn("This is a sensor", turn.text)
        await self.drain(runtime)
        goal = await runtime.store.get(turn.goal_id)
        self.assertEqual(goal.status, "completed")
        self.assertEqual(len(runtime.actions.calls), 2)
        for role, data, _ in runtime.inference.calls:
            if role == "decide" and data["goal"] == "Get current news":
                self.assertNotIn("DOCUMENT_ONLY", json.dumps(data))

    async def test_next_action_depends_on_observed_result(self):
        def decide(data):
            if len(data["observations"]) == 0:
                return {"kind": "execute", "action": "news_search", "text": "topic"}
            if len(data["observations"]) == 1:
                self.assertIn("real tool", data["observations"][0]["text"])
                return {
                    "kind": "execute",
                    "action": "summarize",
                    "text": "Summarize findings",
                }
            return auto_decide(data)

        runtime = self.make_runtime({"tasks": [task()]}, decide)
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        self.assertEqual(
            [call[0] for call in runtime.actions.calls], ["news_search", "summarize"]
        )
        self.assertEqual((await runtime.store.get(turn.goal_id)).status, "completed")

    async def test_missing_detail_does_not_block_independent_work(self):
        def decide(data):
            if data["goal"] == "Needs input":
                return {"kind": "ask_user", "text": "Which topic?"}
            return auto_decide(data)

        runtime = self.make_runtime(
            {"tasks": [task("a", "Needs input"), task("b", "Independent work")]}, decide
        )
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        goal = await runtime.store.get(turn.goal_id)
        self.assertEqual([t.status for t in goal.tasks], ["blocked", "completed"])
        self.assertIn("Which topic?", goal.result)

    async def test_dependencies_wait_for_evidence_and_receive_only_required_results(
        self,
    ):
        def decide(data):
            if data["goal"] == "Dependent work":
                self.assertEqual(data["dependencies"][0]["id"], "dependency:a")
                return {
                    "kind": "finish",
                    "text": "Used parent result",
                    "satisfied": True,
                    "evidence": ["dependency:a"],
                }
            return auto_decide(data)

        runtime = self.make_runtime(
            {
                "tasks": [
                    task("a", "Parent work"),
                    task("b", "Dependent work", depends_on=["a"]),
                ]
            },
            decide,
        )
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        goal = await runtime.store.get(turn.goal_id)
        self.assertEqual(goal.status, "completed")
        self.assertEqual(len(runtime.actions.calls), 1)

    async def test_clarification_persists_and_resumes_after_runtime_recreation(self):
        runtime = self.make_runtime(
            {"tasks": [task()]},
            lambda data: {"kind": "ask_user", "text": "Which node?"},
        )
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        recovered = self.make_runtime()
        await recovered.control(
            turn.goal_id, "u", "s", "resume", task_id="t1", feedback="Inspect node-a"
        )
        await self.drain(recovered)
        goal = await recovered.store.get(turn.goal_id)
        self.assertEqual(goal.status, "completed")
        self.assertIn("Inspect node-a", goal.tasks[0].spec.context)

    async def test_corrective_feedback_reopens_completed_work_and_invalidates_dependents(
        self,
    ):
        runtime = self.make_runtime(
            {"tasks": [task("a", "Parent"), task("b", "Dependent", depends_on=["a"])]}
        )
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        self.assertEqual(len(runtime.actions.calls), 2)
        await runtime.control(
            turn.goal_id,
            "u",
            "s",
            "resume",
            task_id="a",
            feedback="Verify against updated data",
        )
        await self.drain(runtime)
        goal = await runtime.store.get(turn.goal_id)
        self.assertEqual(goal.status, "completed")
        self.assertEqual(len(runtime.actions.calls), 4)
        self.assertEqual([t.cycles for t in goal.tasks], [1, 1])

    async def test_uncertain_in_flight_action_is_not_replayed_on_restart(self):
        runtime = self.make_runtime()
        goal = Goal(
            user_id="u",
            session_id="s",
            tasks=[
                TaskState(
                    spec=TaskSpec(**task()),
                    status="running",
                    pending_action="list_nodes",
                    pending_work_id="uncertain",
                )
            ],
        )
        await runtime.store.save(goal)
        await runtime.sweep()
        await self.drain(runtime)
        recovered = await runtime.store.get(goal.id)
        self.assertEqual(recovered.status, "needs_input")
        self.assertEqual(runtime.actions.calls, [])
        self.assertIn("reconciliation", recovered.tasks[0].answer)

    async def test_completed_result_survives_recreation_without_a_job_record(self):
        runtime = self.make_runtime({"tasks": [task()]})
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        recovered = self.make_runtime()
        result = await recovered.control(turn.goal_id, "u", "s", "result")
        self.assertIn("Verified", result)
        self.assertEqual(recovered.jobs.list_jobs(), [])
        goal = await recovered.store.get(turn.goal_id)
        self.assertTrue(goal.notices)
        self.assertIn("Verified", goal.notices[-1].text)

    async def test_goal_controls_are_scoped_to_user_and_session(self):
        runtime = self.make_runtime({"tasks": [task(event_key="event")]})
        turn = await runtime.handle_turn(request())
        for user, session in [("other", "s"), ("u", "other")]:
            with self.assertRaises(ValueError):
                await runtime.control(turn.goal_id, user, session, "cancel")
        self.assertEqual((await runtime.store.get(turn.goal_id)).status, "waiting")

    async def test_recurring_goal_wakes_and_does_not_accept_old_cycle_evidence(self):
        runtime = self.make_runtime({"tasks": [task(repeat_seconds=60)]})
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        goal = await runtime.store.get(turn.goal_id)
        self.assertEqual(goal.status, "waiting")
        self.assertEqual(goal.tasks[0].cycles, 1)
        self.assertEqual(await runtime.store.list(due_only=True), [])
        goal.tasks[0].next_wake_at = time.time() - 1
        goal.next_wake_at = goal.tasks[0].next_wake_at
        await runtime.store.save(goal)
        await runtime.sweep()
        await self.drain(runtime)
        goal = await runtime.store.get(turn.goal_id)
        self.assertEqual(goal.tasks[0].cycles, 2)
        self.assertEqual(len(runtime.actions.calls), 2)
        self.assertEqual(goal.status, "waiting")

    async def test_event_subscription_is_dormant_until_matching_event(self):
        runtime = self.make_runtime({"tasks": [task(event_key="new-data")]})
        turn = await runtime.handle_turn(request())
        self.assertIsNone(turn.job_id)
        self.assertEqual(await runtime.store.list(due_only=True), [])
        with self.assertRaises(ValueError):
            await runtime.control(turn.goal_id, "u", "s", "event", event_key="wrong")
        await runtime.control(
            turn.goal_id,
            "u",
            "s",
            "event",
            event_key="new-data",
            feedback="new observation arrived",
        )
        await self.drain(runtime)
        self.assertEqual(len(runtime.actions.calls), 1)
        self.assertEqual((await runtime.store.get(turn.goal_id)).status, "waiting")

    async def test_future_task_is_persisted_without_early_execution(self):
        runtime = self.make_runtime({"tasks": [task(start_at=time.time() + 1000)]})
        turn = await runtime.handle_turn(request())
        self.assertIsNone(turn.job_id)
        self.assertEqual(await runtime.store.list(due_only=True), [])

    async def test_unavailable_capability_is_not_executed_or_reported_as_success(self):
        runtime = self.make_runtime(
            {"tasks": [task()]},
            lambda data: {"kind": "execute", "action": "delete_pod"},
        )
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        goal = await runtime.store.get(turn.goal_id)
        self.assertEqual(goal.status, "needs_input")
        self.assertEqual(runtime.actions.calls, [])

    async def test_failed_action_is_an_observation_and_can_lead_to_partial_result(self):
        def fail(*args):
            raise OSError("Tool unavailable")

        def decide(data):
            if data["observations"]:
                self.assertEqual(data["observations"][-1]["status"], "failed")
                return {
                    "kind": "finish",
                    "text": "Unable to verify current facts",
                    "satisfied": False,
                }
            return auto_decide(data)

        runtime = self.make_runtime({"tasks": [task()]}, decide, FakeActions(fail))
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        self.assertEqual((await runtime.store.get(turn.goal_id)).status, "failed")

    async def test_completion_requires_real_evidence(self):
        runtime = self.make_runtime(
            {"tasks": [task()]},
            lambda data: {
                "kind": "finish",
                "text": "Done",
                "satisfied": True,
                "evidence": ["invented"],
            },
        )
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        self.assertEqual((await runtime.store.get(turn.goal_id)).status, "needs_input")

    async def test_step_limit_and_repeated_actions_stop(self):
        for max_steps in (1, 6):
            runtime = self.make_runtime(
                {"tasks": [task()]},
                lambda data: {"kind": "execute", "action": "list_nodes"},
                limits=AgentLimits(max_steps_per_task=max_steps),
            )
            turn = await runtime.handle_turn(request())
            await self.drain(runtime)
            self.assertEqual(len(runtime.actions.calls), 1)
            self.assertEqual(
                (await runtime.store.get(turn.goal_id)).status, "needs_input"
            )

    async def test_cancelled_goal_is_not_resumed_by_watcher(self):
        entered = asyncio.Event()

        async def hang(*args):
            entered.set()
            await asyncio.Event().wait()

        runtime = self.make_runtime({"tasks": [task()]}, actions=FakeActions(hang))
        turn = await runtime.handle_turn(request())
        await entered.wait()
        await runtime.control(turn.goal_id, "u", "s", "cancel")
        self.assertEqual((await runtime.store.get(turn.goal_id)).status, "cancelled")
        await runtime.sweep()
        self.assertEqual(await runtime.store.list(due_only=True), [])

    async def test_shutdown_checkpoints_interrupted_action_for_safe_recovery(self):
        entered = asyncio.Event()

        async def hang(*args):
            entered.set()
            await asyncio.Event().wait()

        runtime = self.make_runtime({"tasks": [task()]}, actions=FakeActions(hang))
        turn = await runtime.handle_turn(request())
        await entered.wait()
        await runtime.close()
        recovered = self.make_runtime()
        await recovered.sweep()
        await self.drain(recovered)
        self.assertEqual(
            (await recovered.store.get(turn.goal_id)).status, "needs_input"
        )
        self.assertEqual(recovered.actions.calls, [])

    async def test_token_exhaustion_prevents_further_actions(self):
        runtime = self.make_runtime(
            {"tasks": [task()]}, limits=AgentLimits(token_budget=1024)
        )
        original = runtime.inference.generate

        async def expensive(role, messages, **kw):
            if kw.get("charge"):
                await kw["charge"](2000)
            return await original(role, messages, **kw)

        runtime.inference.generate = expensive
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        self.assertEqual(runtime.actions.calls, [])
        self.assertEqual((await runtime.store.get(turn.goal_id)).status, "needs_input")

    async def test_updates_can_retrieve_prior_work_in_natural_turn(self):
        runtime = self.make_runtime({"tasks": [task()]})
        turn = await runtime.handle_turn(request())
        await self.drain(runtime)
        runtime.inference.plan = {
            "updates": [{"goal_id": turn.goal_id, "action": "result"}]
        }
        response = await runtime.handle_turn(request("What did you find?"))
        self.assertIn("Verified", response.text)
        self.assertIsNone(response.goal_id)

    def test_dependencies_and_refs_are_validated(self):
        for tasks in [
            [task("a", depends_on=["missing"])],
            [task("a", depends_on=["a"])],
            [task("a"), task("a")],
            [task("a", repeat_seconds=60), task("b", depends_on=["a"])],
        ]:
            with self.assertRaises(ValidationError):
                TurnPlan(tasks=tasks)
        with self.assertRaises(ValidationError):
            Goal(
                user_id="u",
                session_id="s",
                tasks=[TaskState(spec=TaskSpec(**task(input_refs=["9:9"])))],
            )


class GoalStoreTests(RuntimeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_expired_lease_is_recoverable_and_old_owner_cannot_write(self):
        from unittest.mock import patch

        runtime = self.make_runtime()
        goal = Goal(
            user_id="u", session_id="s", tasks=[TaskState(spec=TaskSpec(**task()))]
        )
        await runtime.store.save(goal)
        first = await runtime.store.claim(goal, "one", 1)
        with patch("time.time", return_value=time.time() + 10):
            due = await runtime.store.list(due_only=True)
            second = await runtime.store.claim(due[0], "two", 60)
            self.assertIsNotNone(second)
            with self.assertRaises(RuntimeError):
                await runtime.store.save(first, owner="one")

    async def test_sqlite_revisions_and_leases_prevent_two_executors(self):
        first, second = self.make_runtime(), self.make_runtime()
        goal = Goal(
            user_id="u", session_id="s", tasks=[TaskState(spec=TaskSpec(**task()))]
        )
        await first.store.save(goal)
        stale = goal.model_copy(deep=True)
        claimed = await first.store.claim(goal, "one", 60)
        self.assertIsNotNone(claimed)
        self.assertIsNone(await second.store.claim(stale, "two", 60))
        with self.assertRaises(RuntimeError):
            await second.store.save(stale)
        claimed.result = "saved"
        await first.store.save(claimed, owner="one", release=True)
        with self.assertRaises(RuntimeError):
            await second.store.save(stale)

    async def test_goal_identity_and_scope_cannot_be_overwritten(self):
        runtime = self.make_runtime()
        goal = Goal(
            user_id="u", session_id="s", tasks=[TaskState(spec=TaskSpec(**task()))]
        )
        await runtime.store.save(goal)
        goal.user_id = "other"
        with self.assertRaises(RuntimeError):
            await runtime.store.save(goal)
        self.assertEqual(await runtime.store.list(user_id="other", session_id="s"), [])


class JobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stage_timeout_covers_runner(self):
        manager = JobManager()

        async def hang(job):
            await asyncio.Event().wait()

        manager.define_pipeline(
            "hang",
            stages=[PipelineStage(name="hang", runner=hang, timeout_seconds=0.01)],
        )
        job = manager.start_pipeline(spec=JobSpec(kind="test"), pipeline="hang")
        with self.assertLogs(level="ERROR"):
            await asyncio.wait_for(manager.tasks[job.job_id], 1)
        self.assertEqual(WorkResult.model_validate(job.get_result()).status, "failed")

    async def test_stage_timeout_covers_stalled_poller(self):
        manager = JobManager()

        async def start(job):
            return WorkResult(status="accepted", work_id=job.job_id)

        async def poll(job, result):
            await asyncio.Event().wait()

        manager.define_pipeline(
            "poll",
            stages=[
                PipelineStage(
                    name="poll",
                    runner=start,
                    poller=poll,
                    timeout_seconds=0.02,
                    poll_interval_seconds=0.001,
                )
            ],
        )
        job = manager.start_pipeline(spec=JobSpec(kind="test"), pipeline="poll")
        with self.assertLogs(level="ERROR"):
            await asyncio.wait_for(manager.tasks[job.job_id], 1)
        self.assertEqual(job.status, "failed")
