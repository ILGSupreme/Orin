import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import httpx

from common.jobs import JobSpec
from common.primer import Primer
from common.protocol.ingress_types import InferenceSession
from common.protocol.routing_types import CanonicalTask, WorkPacket, WorkResult
from common.protocol.unified_types import ContentPart, RuntimeMessage
from cortex.cortex.harness import Harness
from cortex.cortex.runtime import CortexRuntime
from cortex.cortex.inference import InferenceGateway, InferenceUnavailable
from cortex.cli.command_router import CommandRouter, CommandFolder, Command, ShellState
from test_agent_support import RuntimeFixture, memory_router
from test_inference_gateway import config, response


class HarnessIntegrationTests(RuntimeFixture, unittest.IsolatedAsyncioTestCase):
    def test_inference_failure_returns_safe_503_for_terminal_client(self):
        from fastapi.testclient import TestClient
        from cortex import app as application

        runtime = SimpleNamespace(handle_session_stream=AsyncMock(
            side_effect=InferenceUnavailable("private backend response or credentials")
        ))
        client = TestClient(application.app)
        self.addCleanup(client.close)
        with patch.object(application.app.state, "cortex_runtime", runtime, create=True):
            response = client.post('/terminal_chat', json={
                'user_id': 'u', 'stream': True,
                'content': [RuntimeMessage(role='user', parts=[
                    ContentPart(type='text', data='Hello'),
                ]).model_dump()],
            })
        self.assertEqual(response.status_code, 503)
        self.assertIn('Configured inference', response.json()['detail'])
        self.assertNotIn('private backend', response.text)

    def test_provider_only_api_lifespan_and_chat_without_gpu_initialization(self):
        from fastapi.testclient import TestClient
        from common.system.configuration import CortexConfiguration
        from cortex import app as application

        runtime = self.make_runtime()
        real_client = httpx.AsyncClient

        async def transport(request):
            if request.url.host == "api.openai.com":
                return httpx.Response(
                    200,
                    json=response(
                        json.dumps(
                            {
                                "answers": [{"question": "Greeting", "text": "API provider reply"}],
                                "tasks": [],
                                "updates": [],
                            }
                        )
                    ),
                )
            packet = WorkPacket.model_validate_json(request.content)
            result = await runtime.store.http.runtime.handle_ingress(packet)
            return httpx.Response(200, json=result.model_dump(mode="json"))

        cfg = CortexConfiguration(
            inference=config(), inventory_memory_base_url="http://memory"
        )
        deployment = SimpleNamespace(
            initialize_inventory=AsyncMock(), inventory_status=lambda: {"ok": True}
        )
        discovery = SimpleNamespace(
            selector=SimpleNamespace(
                pick_one=lambda request: SimpleNamespace(
                    name="memory", url="http://memory", work_path="/work"
                )
            ),
            refresher=SimpleNamespace(start=AsyncMock(), stop=AsyncMock()),
        )
        with (
            patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}),
            patch.object(
                application.configuration, "load_configuration_file", return_value=cfg
            ),
            patch.object(
                application.configuration, "get_configuration", return_value=cfg
            ),
            patch.object(application, "DeploymentService", return_value=deployment),
            patch.object(application, "DiscoveryService", return_value=discovery),
            patch.object(
                application.httpx,
                "AsyncClient",
                side_effect=lambda **kw: real_client(
                    transport=httpx.MockTransport(transport), **kw
                ),
            ),
            TestClient(application.app) as client,
        ):
            ready = client.get("/ready")
            self.assertTrue(ready.json()["accepting_requests"])
            self.assertFalse(application.app.state.primer.is_ready())
            reply = client.post(
                "/chat",
                json={
                    "user_id": "u",
                    "content": [
                        RuntimeMessage(
                            role="user", parts=[ContentPart(type="text", data="Hello")]
                        ).model_dump(),
                    ],
                },
            )
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(
                reply.json()["content"][0]["parts"][0]["data"], "API provider reply"
            )
            scoped = client.get(
                "/goals",
                params={"user_id": "u", "session_id": reply.json()["session_id"]},
            )
            self.assertEqual(scoped.json(), {"goals": []})

    async def test_private_conversation_is_not_exported_on_later_external_turn(self):
        from common.protocol.internal_types import InferenceObject
        from cortex.cortex import memory

        runtime = self.make_runtime()
        db = runtime.store.http.runtime._memoryDB
        db.create_chat_message(
            user_id="u",
            session_id="s",
            role="user",
            content="PRIVATE_TEXT",
            content_type="text",
            metadata={"inference_privacy": "local_only"},
        )
        db.create_chat_message(
            user_id="u",
            session_id="s",
            role="assistant",
            content="PUBLIC_TEXT",
            content_type="text",
            metadata={"inference_privacy": "external_allowed"},
        )
        req = InferenceObject(
            user_id="u",
            session_id="s",
            content=[],
            metadata={"privacy": "external_allowed"},
        )
        context = await memory.conversation_context(
            memory_router(runtime.store.http), req, "local_only"
        )
        self.assertIn("PUBLIC_TEXT", str(context))
        self.assertNotIn("PRIVATE_TEXT", str(context))

    async def test_same_unified_flow_for_chat_and_terminal_natural_input(self):
        runtime = self.make_runtime({"reply": "Hello", "tasks": []})
        transport = runtime.store.http
        router = memory_router(transport)
        harness = Harness(
            router=router, job_manager=runtime.jobs, agent_runtime=runtime
        )
        cortex = CortexRuntime(
            harness=harness, discovery_service=None, primer=None, command_router=None
        )
        req = InferenceSession(
            user_id="u",
            content=[
                RuntimeMessage(
                    role="user", parts=[ContentPart(type="text", data="Hello")]
                )
            ],
        )
        chat = await cortex.chat_non_stream(req)
        terminal = await cortex.handle_session_non_stream(req)
        for result in [chat, terminal]:
            self.assertEqual(result.metadata["mode"], "unified")
            self.assertEqual(result.content[0].parts[0].data, "Hello")
            self.assertTrue(result.session_id)

    async def test_provider_only_cortex_without_loaded_local_model(self):
        runtime = self.make_runtime()
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json=response(
                        json.dumps(
                            {"answers": [{"question": "Greeting", "text": "Provider reply"}], "tasks": [], "updates": []}
                        )
                    ),
                )
            )
        )
        self.addAsyncCleanup(http.aclose)
        primer = Primer(external_http=http, cfg="cortex")
        runtime.inference = InferenceGateway(
            config(), primer=primer, router=None, http=http
        )
        harness = Harness(
            router=memory_router(runtime.store.http),
            job_manager=runtime.jobs,
            agent_runtime=runtime,
        )
        cortex = CortexRuntime(
            harness=harness, discovery_service=None, primer=primer, command_router=None
        )
        req = InferenceSession(
            user_id="u",
            metadata={"privacy": "external_allowed"},
            content=[
                RuntimeMessage(
                    role="user", parts=[ContentPart(type="text", data="Hello")]
                )
            ],
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            result = await cortex.chat_non_stream(req)
        self.assertFalse(primer.is_ready())
        self.assertEqual(result.content[0].parts[0].data, "Provider reply")

    async def test_direct_work_ingress_rejects_agent_tool_and_memory_requests(self):
        runtime = self.make_runtime()
        router = SimpleNamespace(send=AsyncMock())
        harness = Harness(
            router=router, job_manager=runtime.jobs, agent_runtime=runtime
        )
        for work_type in ["tool", "memory", "cortex"]:
            result = await harness.handle_work_packet(
                WorkPacket(
                    work_id="external",
                    metadata={"pipeline": "agent"},
                    task=CanonicalTask(work_type=work_type, operation="list_nodes"),
                )
            )
            self.assertEqual(result.status, "failed")
        router.send.assert_not_awaited()

    async def test_direct_llm_result_is_sanitized_and_correlated(self):
        runtime = self.make_runtime()
        router = SimpleNamespace(
            send=AsyncMock(
                return_value=WorkResult(
                    work_id="backend-work",
                    status="completed",
                    backend_name="internal-pod",
                    metadata={"backend_ref": {"url": "http://private"}},
                )
            )
        )
        harness = Harness(
            router=router, job_manager=runtime.jobs, agent_runtime=runtime
        )
        accepted = await harness.handle_work_packet(
            WorkPacket(
                work_id="origin", task=CanonicalTask(work_type="llm", operation="chat")
            )
        )
        await self.drain(runtime)
        result = await harness.retrieve_work_packet(accepted.work_id)
        self.assertEqual(result.work_id, accepted.work_id)
        self.assertIsNone(result.backend_name)
        self.assertEqual(result.metadata, {})

    async def test_explicit_agent_actions_ignore_selected_backend(self):
        calls = []

        async def inspect(ctx, args):
            calls.append((ctx.backend, args))
            return "ready"

        router = CommandRouter.__new__(CommandRouter)
        router.root_folder = CommandFolder(
            name="Root",
            desc="test",
            commands=[
                Command(
                    command="/list_nodes",
                    desc="inspect",
                    kind="text",
                    handler=inspect,
                    modes={"terminal"},
                    harness_action="list_nodes",
                    safe_info_action=True,
                )
            ],
        )
        router.shell_state = ShellState(selected_backend_name="unrelated")
        self.assertEqual(await router.execute_agent_action("list_nodes"), "ready")
        self.assertEqual(calls, [(None, "")])
        with self.assertRaises(ValueError):
            await router.execute_agent_action("delete_pod")
        router.root_folder.commands[0].suggest_only = True
        with self.assertRaises(ValueError):
            await router.execute_agent_action("list_nodes")

    async def test_job_cancel_command_cancels_durable_goal(self):
        runtime = self.make_runtime()
        router = CommandRouter.__new__(CommandRouter)
        router.job_manager = runtime.jobs
        router.agent_runtime = SimpleNamespace(
            control=AsyncMock(return_value="Cancelled durable goal")
        )
        job = runtime.jobs._create_job(
            JobSpec(
                kind="cortex.turn",
                payload={"goal_id": "goal:one", "user_id": "u", "session_id": "s"},
            )
        )
        result = await router.handle_cancel_job(None, job.job_id)
        self.assertIn("Cancelled", result)
        router.agent_runtime.control.assert_awaited_once_with(
            "goal:one", "u", "s", "cancel"
        )
