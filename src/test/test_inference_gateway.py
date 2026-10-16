import json
import asyncio
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

import httpx
from pydantic import BaseModel, ConfigDict

from common.primer import Primer
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import (
    ContentPart,
    RuntimeMessage,
    embed_system_message_user_dict,
)
from common.system.configuration import InferenceConfiguration
from cortex.cortex.inference import InferenceGateway, InferenceUnavailable


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reply: str


class LocalGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_generation_and_timeout_on_supported_python_versions(self):
        cfg = InferenceConfiguration(
            backends={"local": {"kind": "primer", "timeout_seconds": 0.02}},
            bindings={role: ["local"] for role in ("interpret", "decide", "work")},
        )
        primer = SimpleNamespace(
            is_ready=lambda: True,
            _get_total_token_estimation=lambda **kw: 100,
            status=lambda: {"effective_n_ctx": 4096},
            chat_json=AsyncMock(return_value={"reply": "Hello"}),
        )
        gateway = InferenceGateway(cfg, primer=primer, router=None, http=None)
        result = await gateway.generate("interpret", [], privacy="local_only", model=Output)
        self.assertEqual(Output.model_validate_json(result.text).reply, "Hello")

        async def stalled(**kwargs):
            await asyncio.Event().wait()

        primer.chat_json = stalled
        with self.assertLogs("cortex.cortex.inference", level="WARNING") as logs:
            with self.assertRaises(InferenceUnavailable):
                await gateway.generate("interpret", [], privacy="local_only", model=Output)
        self.assertIn("error_type=TimeoutError", logs.output[0])


def config(**extra):
    return InferenceConfiguration(
        **{
            "allow_external": True,
            "default_privacy": "external_allowed",
            "backends": {
                "provider": {
                    "kind": "openai",
                    "model": "test-model",
                    "api_key_env": "TEST_PROVIDER_KEY",
                    "context_tokens": 32000,
                }
            },
            "bindings": {
                role: ["provider"]
                for role in ("interpret", "decide", "respond", "work")
            },
            **extra,
        }
    )


def response(text, **extra):
    return {
        "status": "completed",
        "usage": {"total_tokens": 50},
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        **extra,
    }


class InferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_generation_remains_serial_after_caller_cancellation(self):
        primer = Primer(external_http=None, cfg="cortex")
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def generation():
            calls.append("started")
            entered.set()
            await release.wait()
            return "done"

        first = asyncio.create_task(primer._await_generation(generation))
        await entered.wait()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second = asyncio.create_task(primer._await_generation(generation))
        await asyncio.sleep(0)
        self.assertEqual(calls, ["started"])
        release.set()
        self.assertEqual(await asyncio.wait_for(second, 1), "done")
        self.assertEqual(calls, ["started", "started"])
        self.assertFalse(primer._generation_tasks)

    async def gateway(self, handler, cfg=None):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return InferenceGateway(
            cfg or config(),
            primer=Primer(external_http=http, cfg="cortex"),
            router=None,
            http=http,
        )

    async def test_provider_only_reasoning_uses_responses_and_strict_schema(self):
        captured = []

        def handle(req):
            captured.append(json.loads(req.content))
            self.assertEqual(req.headers["authorization"], "Bearer fake-key")
            return httpx.Response(200, json=response('{"reply":"hello"}'))

        gateway = await self.gateway(handle)
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            self.assertTrue(gateway.status()["ready"])
            result = await gateway.generate(
                "interpret",
                embed_system_message_user_dict("Return JSON", {"question": "hello"}),
                privacy="external_allowed",
                model=Output,
            )
        self.assertEqual(Output.model_validate_json(result.text).reply, "hello")
        body = captured[0]
        self.assertFalse(body["store"])
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertEqual(body["text"]["format"]["schema"]["required"], ["reply"])
        self.assertFalse(gateway.primer.is_ready())

    async def test_local_only_blocks_external_before_transport(self):
        calls = []
        gateway = await self.gateway(lambda req: calls.append(req))
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            with self.assertRaises(InferenceUnavailable):
                await gateway.generate("decide", [], privacy="local_only", model=Output)
        self.assertEqual(calls, [])

    async def test_global_external_permission_is_required(self):
        calls = []
        gateway = await self.gateway(
            lambda req: calls.append(req), config(allow_external=False)
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            with self.assertRaises(InferenceUnavailable):
                await gateway.generate(
                    "interpret", [], privacy="external_allowed", model=Output
                )
        self.assertEqual(calls, [])

    async def test_missing_key_context_budget_and_unsupported_modality_block_calls(
        self,
    ):
        calls = []
        gateway = await self.gateway(lambda req: calls.append(req))
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(InferenceUnavailable):
                await gateway.generate(
                    "interpret", [], privacy="external_allowed", model=Output
                )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            for messages, budget in [
                ([], 1),
                (
                    [
                        RuntimeMessage(
                            role="user", parts=[ContentPart(type="audio", data="bytes")]
                        )
                    ],
                    30000,
                ),
                (
                    [
                        RuntimeMessage(
                            role="user",
                            parts=[ContentPart(type="text", data="x" * 40000)],
                        )
                    ],
                    100000,
                ),
            ]:
                with self.assertRaises(InferenceUnavailable):
                    await gateway.generate(
                        "interpret",
                        messages,
                        privacy="external_allowed",
                        model=Output,
                        budget=budget,
                    )
        self.assertEqual(calls, [])

    async def test_incomplete_refusal_invalid_json_and_http_failures_are_not_success(
        self,
    ):
        variants = [
            httpx.Response(200, json=response('{"reply":"ok"}', status="incomplete")),
            httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "refusal"}]}],
                },
            ),
            httpx.Response(200, json=response("not-json")),
            httpx.Response(429, json={"error": "DO_NOT_LEAK_ERROR_BODY"}),
        ]
        for variant in variants:
            gateway = await self.gateway(lambda req, v=variant: v)
            with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
                with self.assertRaises(InferenceUnavailable) as raised:
                    await gateway.generate(
                        "decide", [], privacy="external_allowed", model=Output
                    )
            self.assertNotIn("DO_NOT_LEAK", str(raised.exception))
            self.assertNotIn("fake-key", str(raised.exception))

    async def test_redirects_are_not_followed(self):
        calls = []

        def handler(req):
            calls.append(req.url)
            return httpx.Response(
                307, headers={"location": "https://another-host/steal"}
            )

        gateway = await self.gateway(handler)
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            with self.assertRaises(InferenceUnavailable):
                await gateway.generate(
                    "interpret", [], privacy="external_allowed", model=Output
                )
        self.assertEqual(len(calls), 1)

    async def test_image_input_adapter_preserves_data_and_requires_capability(self):
        captured = []

        def handler(req):
            captured.append(json.loads(req.content))
            return httpx.Response(200, json=response("image description"))

        cfg = config()
        cfg.backends["provider"].modalities = ["text", "image"]
        gateway = await self.gateway(handler, cfg)
        messages = [
            RuntimeMessage(
                role="user",
                parts=[
                    ContentPart(
                        type="image",
                        data="AAAA",
                        encoding="base64",
                        mime_type="image/png",
                    )
                ],
            )
        ]
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            await gateway.generate("work", messages, privacy="external_allowed")
        block = captured[0]["input"][0]["content"][0]
        self.assertEqual(
            block, {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        )

    async def test_allocation_is_charged_before_provider_effects(self):
        events = []

        def handler(req):
            events.append("http")
            return httpx.Response(200, json=response('{"reply":"ok"}'))

        gateway = await self.gateway(handler)

        async def charge(tokens):
            events.append("charge")

        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "fake-key"}):
            await gateway.generate(
                "interpret", [], privacy="external_allowed", model=Output, charge=charge
            )
        self.assertEqual(events[:2], ["charge", "http"])

    async def test_routed_inference_uses_workpacket_and_polls(self):
        cfg = InferenceConfiguration()
        cfg.bindings = {role: ["cluster"] for role in cfg.bindings}
        router = SimpleNamespace(
            backend_services=SimpleNamespace(find_candidates=lambda **kw: [object()]),
            send=AsyncMock(
                return_value=WorkResult(
                    work_id="remote-id",
                    status="accepted",
                    metadata={"backend_ref": {"name": "backend"}},
                )
            ),
            retrieve_completed_work=AsyncMock(
                return_value=WorkResult(
                    work_id="remote-id",
                    status="completed",
                    content=[
                        RuntimeMessage(
                            role="assistant",
                            parts=[ContentPart(type="text", data='{"reply":"routed"}')],
                        )
                    ],
                )
            ),
        )
        gateway = InferenceGateway(cfg, primer=None, router=router, http=None)
        result = await gateway.generate(
            "decide", [], privacy="local_only", model=Output
        )
        self.assertEqual(Output.model_validate_json(result.text).reply, "routed")
        self.assertEqual(router.send.call_args.args[0].task.work_type, "llm")
        router.retrieve_completed_work.assert_awaited_once()

    def test_core_imports_do_not_require_gpu_modules(self):
        source = """import sys
from common.primer import Primer
from common.protocol.routing_types import WorkResult
from cortex.cortex.harness import Harness
from cortex.cortex.runtime import CortexRuntime
assert not {'torch','llama_cpp','vllm'} & sys.modules.keys()
"""
        subprocess.run(
            [sys.executable, "-c", source],
            check=True,
            env=os.environ.copy(),
            capture_output=True,
        )
