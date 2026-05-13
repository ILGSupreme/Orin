from __future__ import annotations

from common.protocol.routing_types import (
    WorkPacket,
)
from cortex.cluster.discovery.policy import BackendRoutingPolicy


class Planner:
    def __init__(self, routing_policy: BackendRoutingPolicy) -> None:
        self.routing_policy = routing_policy

    def select_backend(self, packet: WorkPacket):
        return self.routing_policy.select_backend(
            role=packet.required_role,
            required_capabilities=packet.required_capabilities,
            required_modalities=packet.required_modalities,
            runtime_preference=packet.routing_hints.runtime_preference,
        )
