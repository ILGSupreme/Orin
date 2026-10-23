"""Persistent goals and one adaptive execution flow for natural interactions."""

from __future__ import annotations

import asyncio
from async_timeout import timeout
import json
import logging
import hashlib
import time
from dataclasses import dataclass
from uuid import uuid4

from common.jobs import JobSpec
from common.protocol.agent_types import (
    AgentLimits,
    decision_for_evidence,
    Goal,
    GoalNotice,
    Observation,
    TaskState,
    TurnPlan,
)
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import ContentPart, RuntimeMessage
from cortex.cortex.prompts import decision_messages, turn_messages

logger = logging.getLogger(__name__)


@dataclass
class TurnResponse:
    text: str
    goal_id: str | None = None
    job_id: str | None = None
    privacy: str = "local_only"


class AgentRuntime:
    def __init__(
        self,
        *,
        inference,
        actions,
        store,
        jobs,
        limits=None,
        default_privacy="local_only",
        context_loader=None,
    ):
        self.inference, self.actions, self.store, self.jobs = (
            inference,
            actions,
            store,
            jobs,
        )
        self.limits = limits or AgentLimits()
        self.default_privacy, self.context_loader = default_privacy, context_loader
        self.owner = "cortex:" + uuid4().hex
        self.running = {}
        self._control_lock = asyncio.Lock()
        self._scheduler = None
        self.storage_error = None

    def start(self):
        self._scheduler = asyncio.create_task(self._watch())

    async def close(self):
        if self._scheduler:
            self._scheduler.cancel()
            await asyncio.gather(self._scheduler, return_exceptions=True)
        # Workers checkpoint interruption while Memory HTTP is still available.
        tasks = []
        for job in list(self.running.values()):
            task = self.jobs.tasks.get(job.job_id)
            if task:
                tasks.append(task)
            self.jobs.cancel(job.job_id)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle_turn(self, request):
        if not request.session_id:
            raise ValueError("A persistent session is required")
        privacy = request.metadata.get("privacy", self.default_privacy)
        if privacy not in {"local_only", "external_allowed"}:
            raise ValueError("Unknown privacy policy")
        turn_id = request.metadata.get("turn_id")
        stable_id = None
        if turn_id is not None:
            if not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128:
                raise ValueError(
                    "turn_id must be a nonempty string of at most 128 characters"
                )
            stable_id = (
                "goal:"
                + hashlib.sha256(
                    json.dumps(
                        [request.user_id, request.session_id, turn_id],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            )
            previous = await self.store.get(
                stable_id, request.user_id, request.session_id
            )
            if previous:
                running = self.running.get(previous.id)
                return TurnResponse(
                    previous.result or f"Work already recorded ({previous.status}).",
                    previous.id,
                    running.job_id if running else None,
                    previous.privacy,
                )
        goals = await self.store.list(
            user_id=request.user_id, session_id=request.session_id
        )
        # A later provider-enabled turn must not silently export private goals.
        if privacy == "external_allowed":
            goals = [goal for goal in goals if goal.privacy == "external_allowed"]
        # The interpreter sees text and input references, not attachment bytes.
        inputs = []
        for i, message in enumerate(request.content):
            for j, part in enumerate(message.parts):
                inputs.append(
                    {
                        "ref": f"{i}:{j}",
                        "type": part.type,
                        "text": part.data[: 1600 if j == 0 else 160]
                        if part.type in {"text", "json"}
                        else "",
                        "truncated": len(part.data) > (1600 if j == 0 else 160),
                    }
                )
        context = await self.context_loader(request) if self.context_loader else []
        result = await self.inference.generate(
            "interpret",
            turn_messages(
                {
                    "inputs": inputs,
                    "conversation": context,
                    "now": time.time(),
                    "actions": self.actions.available(),
                    "goals": [
                        {
                            "id": g.id,
                            "status": g.status,
                            "tasks": [
                                {
                                    "id": t.spec.id,
                                    "goal": t.spec.goal[:120],
                                    "status": t.status,
                                    "answer": t.answer[:160],
                                }
                                for t in g.tasks[:4]
                            ],
                        }
                        for g in goals[:2]
                    ],
                }
            ),
            privacy=privacy,
            model=TurnPlan,
            budget=self.limits.token_budget,
            max_output_tokens=512,
        )
        plan = TurnPlan.model_validate_json(result.text)
        # Work acknowledgements come from runtime outcomes, never interpreter
        # prose. Preserve a separately identified answer for mixed requests.
        immediate_reply = (
            plan.reply
            if not (plan.tasks or plan.updates)
            or plan.reply_scope == "independent_answer"
            else ""
        )
        logger.info(
            "Turn interpreted backend=%s tasks=%d updates=%d has_reply=%s "
            "reply_scope=%s reply_suppressed=%s",
            result.backend, len(plan.tasks), len(plan.updates), bool(plan.reply),
            plan.reply_scope, bool(plan.reply and not immediate_reply),
        )
        replies = [immediate_reply] if immediate_reply else []
        reply_privacy = privacy
        for update in plan.updates:
            referenced = await self.store.get(
                update.goal_id, request.user_id, request.session_id
            )
            if referenced is not None and referenced.privacy == "local_only":
                reply_privacy = "local_only"
            replies.append(
                await self.control(
                    update.goal_id,
                    request.user_id,
                    request.session_id,
                    update.action,
                    task_id=update.task_id,
                    feedback=update.feedback,
                )
            )
        if not plan.tasks:
            return TurnResponse(
                "\n\n".join(replies) or "What would you like me to do?",
                privacy=reply_privacy,
            )
        now = time.time()
        tasks = []
        for spec in plan.tasks:
            waiting = bool(spec.event_key or (spec.start_at and spec.start_at > now))
            tasks.append(
                TaskState(
                    spec=spec,
                    status="waiting" if waiting else "ready",
                    next_wake_at=spec.start_at
                    if waiting and not spec.event_key
                    else None,
                )
            )
        goal = Goal(
            user_id=request.user_id,
            session_id=request.session_id,
            privacy=privacy,
            tasks=tasks,
            messages=request.content,
            reply=immediate_reply,
            allocated_tokens=result.tokens,
        )
        if stable_id:
            goal.id = stable_id
        self._settle(goal)
        try:
            await self.store.save(goal)
        except RuntimeError:
            previous = await self.store.get(
                goal.id, request.user_id, request.session_id
            )
            if not previous:
                raise
            # A committed create may have lost its response, or another ingress
            # may have accepted the same client turn. Do not create duplicate work.
            goal = previous
        job = await self._start(goal)
        replies.append(
            "I’ve recorded the requested work."
            if not job
            else "I’m working on the remaining requests."
        )
        return TurnResponse(
            "\n\n".join(replies), goal.id, job.job_id if job else None, reply_privacy
        )

    async def _start(self, goal):
        async with self._control_lock:
            if goal.id in self.running:
                return self.running[goal.id]
            if len(self.running) >= self.limits.max_active_runs:
                return None  # Persisted ready state will be picked up by the watcher.
            if goal.status not in {"ready", "running", "waiting"}:
                return None
            if goal.status == "waiting" and (
                goal.next_wake_at is None or goal.next_wake_at > time.time()
            ):
                return None
            job = self.jobs.start(
                spec=JobSpec(
                    kind="cortex.turn",
                    payload={
                        "goal_id": goal.id,
                        "user_id": goal.user_id,
                        "session_id": goal.session_id,
                    },
                ),
                runner=lambda job: self._run(job, goal),
            )
            self.running[goal.id] = job
            return job

    async def sweep(self):
        for goal in await self.store.list(due_only=True):
            await self._start(goal)

    async def _watch(self):
        while True:
            try:
                await self.sweep()
                self.storage_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.storage_error is None:
                    logger.warning("Goal recovery/wake-up deferred: Memory unavailable")
                self.storage_error = type(exc).__name__
            await asyncio.sleep(self.limits.poll_seconds)

    async def _save(self, goal, release=False):
        return await self.store.save(goal, owner=self.owner, release=release)

    async def _run(self, job, candidate):
        goal = None
        try:
            goal = await self.store.claim(
                candidate, self.owner, self.limits.slice_seconds + 30
            )
            if goal is None:
                return WorkResult(
                    work_id=job.job_id,
                    status="completed",
                    metadata={"claimed_elsewhere": True},
                )
            for task in goal.tasks:
                if task.pending_action:
                    self._observe(
                        task,
                        "recovery",
                        "failed",
                        "An action was interrupted with an unknown outcome. Reconcile it before resuming.",
                        task.pending_work_id,
                    )
                    task.status, task.answer = (
                        "blocked",
                        "Interrupted action requires reconciliation.",
                    )
                elif (
                    task.status == "waiting"
                    and task.next_wake_at is not None
                    and task.next_wake_at <= time.time()
                ):
                    task.status, task.next_wake_at = "ready", None
                    task.steps = 0
                    task.action_history = []
                elif task.status == "running":
                    task.status = "ready"
            self._settle(goal)
            await self._save(goal)
            async with timeout(self.limits.slice_seconds):
                while True:
                    ready = [
                        task
                        for task in goal.tasks
                        if task.status == "ready"
                        and self._dependencies_ready(goal, task)
                    ]
                    if not ready:
                        break
                    # Round-robin steps prevent a blocked task from stopping unrelated work.
                    for task in ready:
                        await self._step(job, goal, task)
                        self._settle(goal)
                        await self._save(goal)
            self._settle(goal)
            await self._save(goal, release=True)
            return self._result(job, goal)
        except asyncio.CancelledError:
            if goal:
                for task in goal.tasks:
                    if task.status == "running":
                        task.status = "ready"
                self._settle(goal)
                try:
                    await self._save(goal, release=True)
                except Exception:
                    logger.warning(
                        "Interrupted goal will recover after its lease expires"
                    )
            raise
        except asyncio.TimeoutError:
            if goal:
                for task in goal.tasks:
                    if task.status == "running":
                        task.status = "blocked" if task.pending_action else "ready"
                        task.answer = "Execution time slice expired; reconcile any pending action before resuming."
                self._settle(goal)
                await self._save(goal, release=True)
                return self._result(job, goal)
            raise
        finally:
            self.running.pop(candidate.id, None)

    async def _step(self, job, goal, task):
        if task.steps >= self.limits.max_steps_per_task:
            task.status, task.answer = (
                "blocked",
                "Step limit reached; further work needs your instruction.",
            )
            return
        task.status = "running"
        goal.status = "running"
        task.steps += 1
        job.progress_message = (
            f"{task.spec.id}: step {task.steps}, {task.spec.goal[:120]}"
        )
        await self._save(goal)
        dependencies = [
            {"id": "dependency:" + t.spec.id, "text": t.answer[:1000]}
            for t in goal.tasks
            if t.spec.id in task.spec.depends_on and t.status == "completed"
        ]
        evidence_ids = {
            o.id
            for o in task.observations
            if o.status == "completed" and o.cycle == task.cycles
        }
        evidence_ids.update(item["id"] for item in dependencies)
        decision_model = decision_for_evidence(sorted(evidence_ids))

        async def charge(tokens):
            goal.allocated_tokens += tokens
            await self._save(goal)
            if goal.allocated_tokens > self.limits.token_budget:
                raise ValueError("Goal token allocation exhausted")

        try:
            result = await self.inference.generate(
                "decide",
                decision_messages(
                    {
                        "goal": task.spec.goal,
                        "criteria": task.spec.criteria,
                        "context": task.spec.context,
                        "input_refs": task.spec.input_refs,
                        "dependencies": dependencies,
                        "eligible_evidence_ids": sorted(evidence_ids),
                        "actions": self.actions.available(),
                        "cycle": task.cycles,
                        "remaining_steps": self.limits.max_steps_per_task
                        - task.steps
                        + 1,
                        "observations": [
                            o.model_dump() | {"text": o.text[:600]}
                            for o in task.observations[-4:]
                        ],
                    }
                ),
                privacy=goal.privacy,
                model=decision_model,
                max_output_tokens=512,
                budget=self.limits.token_budget - goal.allocated_tokens,
                charge=charge,
            )
            decision = decision_model.model_validate_json(result.text)
            if decision.kind == "ask_user":
                if not decision.text:
                    raise ValueError("Clarification requires a question")
                task.status, task.answer = "blocked", decision.text
            elif decision.kind == "finish":
                successful = {
                    o.id
                    for o in task.observations
                    if o.status == "completed" and o.cycle == task.cycles
                }
                successful.update(item["id"] for item in dependencies)
                if not decision.text or (
                    decision.satisfied
                    and (
                        not decision.evidence
                        or not set(decision.evidence) <= successful
                    )
                ):
                    raise ValueError(
                        "Completion requires an answer and valid successful evidence"
                    )
                task.answer = decision.text
                task.status = "completed" if decision.satisfied else "failed"
                if task.spec.repeat_seconds:
                    task.status = "waiting"
                    task.next_wake_at = time.time() + task.spec.repeat_seconds
                    task.cycles += 1
                elif task.spec.event_key:
                    task.status = "waiting"
                    task.cycles += 1
            else:
                signature = json.dumps([decision.action, decision.text], sort_keys=True)
                if signature in task.action_history:
                    raise ValueError("Identical action already attempted this cycle")
                if decision.action not in self.actions.available():
                    raise ValueError("Action unavailable or prohibited")
                task.action_history.append(signature)
                task.pending_action = decision.action
                task.pending_work_id = "action:" + uuid4().hex
                await self._save(goal)  # Write-ahead checkpoint before any effect.

                async def checkpoint(work_id):
                    task.pending_work_id = work_id
                    await self._save(goal)

                messages = [
                    RuntimeMessage(
                        role="user",
                        parts=[
                            ContentPart(
                                type="text",
                                data=json.dumps(
                                    {
                                        "goal": task.spec.goal,
                                        "context": task.spec.context,
                                        "dependencies": dependencies,
                                        "observations": [
                                            o.model_dump() | {"text": o.text[:600]}
                                            for o in task.observations[-4:]
                                        ],
                                    }
                                ),
                            )
                        ],
                    )
                ]
                for ref in task.spec.input_refs:
                    i, j = map(int, ref.split(":"))
                    messages.append(
                        RuntimeMessage(role="user", parts=[goal.messages[i].parts[j]])
                    )
                try:
                    observed = await self.actions.execute(
                        decision.action,
                        decision.text,
                        messages=messages,
                        privacy=goal.privacy,
                        budget=self.limits.token_budget - goal.allocated_tokens,
                        checkpoint=checkpoint,
                        charge=charge,
                    )
                    self._observe(
                        task,
                        decision.action,
                        "completed",
                        observed.text,
                        observed.work_id,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    raise
                except Exception as exc:
                    self._observe(
                        task, decision.action, "failed", str(exc), task.pending_work_id
                    )
                task.pending_action, task.pending_work_id = None, None
                task.status = "ready"
        except (asyncio.TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:
            task.status, task.answer = "blocked", str(exc)[:1000]

    def _observe(self, task, action, status, text, work_id=None):
        task.observations = task.observations[-99:]
        task.observations.append(
            Observation(
                action=action,
                status=status,
                text=text[:4000],
                work_id=work_id,
                cycle=task.cycles,
            )
        )

    def _dependencies_ready(self, goal, task):
        by_id = {t.spec.id: t for t in goal.tasks}
        return all(by_id[key].status == "completed" for key in task.spec.depends_on)

    def _settle(self, goal):
        if goal.status == "cancelled":
            return
        for task in goal.tasks:
            if task.blocked_on_dependencies and self._dependencies_ready(goal, task):
                task.status = (
                    "waiting"
                    if task.next_wake_at and task.next_wake_at > time.time()
                    else "ready"
                )
                task.blocked_on_dependencies = False
            if task.status == "ready" and not self._dependencies_ready(goal, task):
                task.status, task.blocked_on_dependencies = "blocked", True
                task.answer = "Waiting for dependency results."
        statuses = {task.status for task in goal.tasks}
        wake = [
            task.next_wake_at
            for task in goal.tasks
            if task.status == "waiting" and task.next_wake_at is not None
        ]
        goal.next_wake_at = None
        if statuses & {"ready", "running"}:
            goal.status = "running" if "running" in statuses else "ready"
        elif "waiting" in statuses:
            goal.status, goal.next_wake_at = "waiting", min(wake) if wake else None
        elif "blocked" in statuses:
            goal.status = "needs_input"
        elif statuses <= {"completed"}:
            goal.status = "completed"
        elif "completed" in statuses:
            goal.status = "partial"
        else:
            goal.status = "failed"
        text = "\n\n".join(f"{t.spec.goal}\n{t.answer}" for t in goal.tasks if t.answer)
        if text and text != goal.result:
            goal.notices = goal.notices[-99:] + [
                GoalNotice(created_at=time.time(), text=text[:6000])
            ]
        goal.result = text

    def _result(self, job, goal):
        return WorkResult(
            work_id=job.job_id,
            status="completed",
            content=[
                RuntimeMessage(
                    role="assistant",
                    parts=[
                        ContentPart(type="text", data=goal.result or "Work recorded.")
                    ],
                )
            ],
            metadata={"goal_id": goal.id, "goal_status": goal.status},
        )

    async def control(
        self,
        goal_id,
        user_id,
        session_id,
        action,
        *,
        task_id=None,
        feedback="",
        event_key=None,
    ):
        async with self._control_lock:
            goal = await self.store.get(goal_id, user_id, session_id)
            if goal is None:
                raise ValueError("No goal belongs to this user and session")
            if action in {"status", "result"}:
                return (
                    goal.result
                    if action == "result" and goal.result
                    else f"{goal.id}: {goal.status}"
                )
            if action not in {"resume", "cancel", "event"}:
                raise ValueError("Unknown goal control action")
            if goal.id in self.running:
                if action != "cancel":
                    raise ValueError(
                        "Goal is executing; wait for its current work to stop"
                    )
                job = self.running[goal.id]
                task = self.jobs.tasks.get(job.job_id)
                self.jobs.cancel(job.job_id)
                if task:
                    await asyncio.gather(task, return_exceptions=True)
                goal = await self.store.get(goal_id, user_id, session_id)
            if action == "cancel":
                goal.status, goal.next_wake_at = "cancelled", None
                for item in goal.tasks:
                    item.status = "cancelled"
                goal.notices = goal.notices[-99:] + [
                    GoalNotice(created_at=time.time(), text="Goal cancelled.")
                ]
                await self.store.save(goal)
                return (
                    f"Cancelled {goal.id}. Already submitted backend work may continue."
                )
            selected = [
                t
                for t in goal.tasks
                if (task_id is None or t.spec.id == task_id)
                and (
                    action != "event"
                    or (t.spec.event_key == event_key and t.status == "waiting")
                )
            ]
            if not selected or (action == "event" and not event_key):
                raise ValueError("No matching task or event subscription")
            if goal.status == "cancelled":
                raise ValueError("Cancelled goals cannot resume; submit a new request")
            changed = set()
            for item in selected:
                if (
                    action == "resume"
                    and item.status not in {"blocked", "failed"}
                    and not (feedback and item.status in {"completed", "waiting"})
                ):
                    continue
                if feedback:
                    self._observe(item, "user_feedback", "user", feedback)
                    item.spec.context = (
                        item.spec.context + "\nUser feedback: " + feedback
                    )[-3000:]
                item.pending_action, item.pending_work_id = None, None
                item.status, item.next_wake_at = "ready", None
                item.steps, item.action_history = 0, []
                item.answer = ""
                item.cycles += 1
                changed.add(item.spec.id)
            if not changed:
                raise ValueError(
                    "No paused task to resume; provide feedback to reopen completed work"
                )
            # Corrective feedback invalidates outcomes built from the old result.
            while True:
                stale = [
                    item
                    for item in goal.tasks
                    if item.spec.id not in changed
                    and set(item.spec.depends_on) & changed
                ]
                if not stale:
                    break
                for item in stale:
                    item.status, item.blocked_on_dependencies = "blocked", True
                    item.answer = "Waiting for updated dependency results."
                    item.steps, item.action_history = 0, []
                    item.cycles += 1
                    changed.add(item.spec.id)
            self._settle(goal)
            await self.store.save(goal)
        await self._start(goal)
        return f"Updated {goal.id}: {goal.status}."
