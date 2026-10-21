"""Agent state is stored through the authoritative Memory WorkPacket service."""

from uuid import uuid4

from common.protocol.agent_types import Goal
from common.protocol.routing_types import (
    CanonicalTask,
    WorkPacket,
    WorkResult,
    WorkType,
)


class GoalStore:
    def __init__(self, *, http, base_url, namespace):
        self.http, self.url, self.namespace = (
            http,
            base_url.rstrip("/") + "/work",
            namespace,
        )

    async def _request(self, operation, **inputs):
        packet = WorkPacket(
            work_id="goals:" + uuid4().hex,
            task=CanonicalTask(
                work_type=WorkType.MEMORY,
                operation=operation,
                inputs={"namespace": self.namespace, **inputs},
            ),
        )
        response = await self.http.post(
            self.url, json=packet.model_dump(mode="json"), timeout=10
        )
        response.raise_for_status()
        result = WorkResult.model_validate(response.json())
        if result.work_id != packet.work_id or result.status != "completed":
            raise RuntimeError("Goal storage unavailable or revision conflict")
        return result.metadata

    async def list(self, *, user_id=None, session_id=None, due_only=False):
        result = await self._request(
            "agent_goals_query",
            user_id=user_id,
            session_id=session_id,
            due_only=due_only,
        )
        return [Goal.model_validate(value) for value in result["goals"]]

    async def get(self, goal_id, user_id=None, session_id=None):
        result = await self._request(
            "agent_goals_query", goal_id=goal_id, user_id=user_id, session_id=session_id
        )
        return Goal.model_validate(result["goals"][0]) if result["goals"] else None

    async def save(self, goal, *, owner=None, release=False):
        result = await self._request(
            "agent_goal_write",
            goal=goal.model_dump(mode="json"),
            expected_revision=goal.revision,
            owner=owner,
            release=release,
        )
        saved = Goal.model_validate(result["goal"])
        goal.revision, goal.updated_at = saved.revision, saved.updated_at
        goal.lease_owner, goal.lease_until = saved.lease_owner, saved.lease_until
        return goal

    async def claim(self, goal, owner, lease_seconds):
        result = await self._request(
            "agent_goal_claim",
            goal_id=goal.id,
            expected_revision=goal.revision,
            owner=owner,
            lease_seconds=lease_seconds,
        )
        return Goal.model_validate(result["goal"]) if result.get("goal") else None
