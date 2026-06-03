import asyncio
from pathlib import Path
from uuid import uuid4

import httpx

from common.factory import packing
from common.protocol.routing_types import RoutingHints, WorkDisposition, WorkType
from common.protocol.unified_types import ContentPart, RuntimeMessage
from common.types import TEMPERATURE_POLICY
from federation.federation_client import FederationClient
from federation.identity import load_or_create_cluster_identity_from_paths
from federation.models import CapabilitySummary

FEDERATION_URL = "http://orin-gw:30081"
NETWORK_SLUG = "public-text-inference"
JOIN_TOKEN = "orin_join_59b162de-1b4c-472e-b2e5-343a556064b1.n1lxsU82i1rA9gdvte2zrmtF4x3Seynm7Vsz5LXdy1M"


async def main() -> None:

    identity = load_or_create_cluster_identity_from_paths(
        private_key_path=Path("./tmp/fed-test/ed25519_private.key"),
        public_key_path=Path("./tmp/fed-test/ed25519_public.key"),
        cluster_id="orin-test-remote-real-1",
    )

    async with httpx.AsyncClient(timeout=30.0) as http:
        client = FederationClient(
            http=http,
            identity=identity,
        )

        networks = await client.list_networks(base_url=FEDERATION_URL)
        print("networks:", networks)

        network = await client.get_network(base_url=FEDERATION_URL, slug=NETWORK_SLUG)
        print("network:", network)

        join_result = await client.join_network(
            base_url=FEDERATION_URL,
            slug=NETWORK_SLUG,
            token=JOIN_TOKEN,
            advertised_capabilities=[],
        )
        print("join:", join_result)

        heartbeat = await client.send_heartbeat(
            base_url=FEDERATION_URL, network_id=join_result.network_id
        )
        print("heartbeat:", heartbeat)

        capabilities = await client.publish_capabilities(
            base_url=FEDERATION_URL,
            network_id=join_result.network_id,
            capabilities=[
                CapabilitySummary(
                    work_type="llm",
                    operations=["chat"],
                    modalities=["text"],
                    max_context_hint=4096,
                    latency_hint="test",
                )
            ],
        )
        print("publish capabilities:", capabilities)

        messages = [
            RuntimeMessage(
                role="user",
                parts=[
                    ContentPart(
                        type="text",
                        data="Hello, is it me you are looking for? Reply in one short sentence.",
                        encoding="plain",
                    )
                ],
            )
        ]

        temperature_policy = TEMPERATURE_POLICY.get("chat", 0.7)

        cpacket = packing.create_canonical_task(
            work_type=WorkType.LLM,
            operation="chat",
            content=messages,
            inputs={},
            constraints={
                "temperature": temperature_policy,
                "stream": False,
                "max_result_tokens": 64,
            },
            routing_hints=RoutingHints(
                role=WorkType.LLM,
                required_capabilities=["chat"],
                required_modalities=["text"],
                runtime_preference=[
                    {"effective_n_ctx": 200},
                ],
            ),
        )

        packet = packing.create_workpacket(
            id=f"federation_smoke:{uuid4().hex}",
            disposition=WorkDisposition.DIRECT,
            metadata={
                "source": "federation_smoke",
                "test": True,
            },
            task=cpacket,
        )

        print("Send packet:", packet.model_dump(mode="json"))

        federation_work_response = await client.submit_work(
            base_url=FEDERATION_URL,
            network_id=join_result.network_id,
            packet=packet.model_dump(mode="json"),
            # Leave this unset for now. The receiving Federation node is the target.
            target_cluster_id=None,
        )

        print("Federation work response:", federation_work_response)

        deadline = asyncio.get_running_loop().time() + 60.0

        while True:
            result = await client.get_work_result(
                base_url=FEDERATION_URL,
                network_id=join_result.network_id,
                work_id=federation_work_response.work_id,
            )

            print("poll:", result.status)

            if result.status in ("completed", "failed"):
                break

            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"Federated work did not finish: {federation_work_response.work_id}")

            await asyncio.sleep(1.0)

        print("GetFederatedWorkResponse:", result)


if __name__ == "__main__":
    asyncio.run(main())
