import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from common.primer import Primer
from common.protocol.ingress_types import LoadModelRequest
from common.system.configuration import PrimerConfiguration
from common.system import profiler
from common.types import GGUFLoadOptions, ModelProfile


class ProfileTests(unittest.TestCase):
    def profile(self, **overrides):
        values = dict(
            total_vram_bytes=3752787968, model_size_bytes=2740937888,
            total_layers=32, n_gpu_layers=[8], block_count=32,
            head_count_kv=4, key_length=256, value_length=256,
            model_max_context=262144, reserve_bytes=1024**3,
            safety_margin_bytes=1024**3, profile_factors={"conservative": 0.5},
        )
        values.update(overrides)
        return profiler.build_profile(**values)["conservative"]

    def test_four_b_explicit_context_independent_of_offload_factor(self):
        result = self.profile(requested_n_ctx=4096)
        self.assertEqual(result.inputs.n_gpu_layers, 8)
        self.assertEqual(result.recommended_n_ctx, 4096)
        self.assertGreaterEqual(result.estimates.estimated_upper_n_ctx, 4096)

    def test_request_exceeding_budget_or_model_limit_is_rejected(self):
        for values in ({"requested_n_ctx": 8192},
                       {"requested_n_ctx": 4096, "model_max_context": 2048}):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "exceeds"):
                self.profile(**values)

    def test_insufficient_memory_is_not_rounded_up_to_minimum(self):
        with self.assertRaisesRegex(ValueError, "Insufficient"):
            self.profile(total_vram_bytes=2 * 1024**3)

    def test_old_4b_configuration_stays_at_1024(self):
        self.assertEqual(self.profile(n_gpu_layers=[16]).recommended_n_ctx, 1024)

    def test_invalid_options_are_rejected_by_api(self):
        for options in ({"n_ctx": 1025}, {"n_ctx": True}, {"n_gpu_layers": -1},
                        {"reserve_bytes": -1}, {"unknown": 2}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                LoadModelRequest(model_id="m", gguf_load=options)


class LoadTests(unittest.IsolatedAsyncioTestCase):
    def make_primer(self):
        primer = Primer(None, "cortex")
        primer.engine_type = "gguf"
        primer._engine = SimpleNamespace(stop=AsyncMock(), load_model=AsyncMock())
        primer.load_model_provider = AsyncMock()
        primer.load_message_adapter = AsyncMock()
        primer.primer_config = lambda: PrimerConfiguration(
            gguf_load=GGUFLoadOptions(n_gpu_layers=8, n_ctx=4096)
        )
        return primer

    async def test_unload_precedes_measurement_and_options_reach_engine(self):
        primer = self.make_primer()
        profile = ModelProfile(recommended_n_ctx=4096)
        def measure(**kwargs):
            primer._engine.stop.assert_awaited_once()
            self.assertEqual(kwargs["n_gpu_layers"], 8)
            self.assertEqual(kwargs["n_ctx"], 4096)
            return {"conservative": profile}
        with patch.object(profiler, "get_model_profile", side_effect=measure):
            await primer.load_model(path="/cached/model.gguf", model_id="m")
        self.assertIs(primer._engine.load_model.await_args.kwargs["profile"], profile)

    async def test_request_options_replace_config_and_failed_profile_does_not_load(self):
        primer = self.make_primer()
        with patch.object(profiler, "get_model_profile",
                          side_effect=ValueError("Insufficient budget")) as measure:
            with self.assertRaisesRegex(ValueError, "Insufficient"):
                await primer.load_model(
                    path="/cached/model.gguf", model_id="m",
                    gguf_load={"n_gpu_layers": 0, "n_ctx": 2048},
                )
        self.assertEqual(measure.call_args.kwargs["n_gpu_layers"], 0)
        primer._engine.load_model.assert_not_awaited()
        self.assertEqual(primer._profile.profiles, {})
        self.assertFalse(primer._generation_lock.locked())

    async def test_cancelled_load_holds_lock_until_native_work_finishes(self):
        primer = self.make_primer()
        entered, release = asyncio.Event(), asyncio.Event()
        async def native_load(**kwargs):
            entered.set()
            await release.wait()
        primer._engine.load_model = native_load
        with patch.object(profiler, "get_model_profile",
                          return_value={"conservative": ModelProfile()}):
            task = asyncio.create_task(primer.load_model(path="/cached/m", model_id="m"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(primer._generation_lock.locked())
            release.set()
            await asyncio.gather(*primer._generation_tasks)
            await asyncio.sleep(0)
            self.assertFalse(primer._generation_lock.locked())
