from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, AsyncIterator, Literal
from common.system import configuration
from transformers import AutoTokenizer
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.engine.async_llm_engine import AsyncLLMEngine
from vllm.sampling_params import SamplingParams

PrimerState = Literal["unloaded", "loading", "ready", "error"]


class VLLMPrimerEngine:
    def __init__(self, cfg: configuration.PrimerConfiguration) -> None:
        self.model_id = cfg.model_id
        self.max_new_tokens = cfg.max_new_tokens
        self.temperature = cfg.temperature
        self.top_p = cfg.top_p
        self.gpu_memory_utilization = cfg.gpu_memory_util
        self.tensor_parallel_size = cfg.tensor_parallel_size
        self.max_model_len = cfg.max_model_len

        self.tokenizer: Any | None = None
        self.engine: AsyncLLMEngine | None = None

        self._state: PrimerState = "unloaded"
        self._error: str | None = None
        self._load_lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._load_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> PrimerState:
        return self._state

    @property
    def error(self) -> str | None:
        return self._error

    def is_ready(self) -> bool:
        return self._state == "ready"

    def is_loading(self) -> bool:
        return self._state == "loading"

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "model_id": self.model_id,
            "error": self._error,
            "gpu_memory_utilization": self.gpu_memory_utilization,
            "tensor_parallel_size": self.tensor_parallel_size,
            "max_model_len": self.max_model_len,
        }

    async def start_background(self) -> None:
        if self._state in {"loading", "ready"}:
            return

        self._state = "loading"
        self._error = None
        self._ready_event.clear()
        self._load_task = asyncio.create_task(self._load())

    async def ensure_ready(self) -> None:
        if self._state == "ready":
            return

        if self._state == "unloaded":
            await self.start_background()

        await self._ready_event.wait()

        if self._state != "ready":
            raise RuntimeError(self._error or "Primer failed to load")

    async def load_model(
        self,
        model_id: str,
        *,
        force_reload: bool = False,
    ) -> None:
        async with self._load_lock:
            if (
                not force_reload
                and self._state == "ready"
                and self.model_id == model_id
                and self.engine is not None
                and self.tokenizer is not None
            ):
                return

            if self._load_task is not None and not self._load_task.done():
                self._load_task.cancel()
                try:
                    await self._load_task
                except asyncio.CancelledError:
                    pass

            self._unload()
            self.model_id = model_id
            self._state = "loading"
            self._error = None
            self._ready_event.clear()

        self._load_task = asyncio.create_task(self._load())
        await self._load_task

    async def stop(self) -> None:
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
            try:
                await self._load_task
            except asyncio.CancelledError:
                pass

        self._unload()

    async def _load(self) -> None:
        async with self._load_lock:
            if self._state == "ready":
                self._ready_event.set()
                return

            try:
                model_id = self.model_id
                logging.info("Loading Primer tokenizer: %s", model_id)
                tokenizer = await asyncio.to_thread(
                    AutoTokenizer.from_pretrained,
                    model_id,
                    trust_remote_code=True,
                )

                logging.info("Loading Primer vLLM engine: %s", model_id)
                engine = await asyncio.to_thread(
                    self._build_engine,
                    model_id,
                )

                self.tokenizer = tokenizer
                self.engine = engine
                self._state = "ready"
                self._error = None
                logging.info("Primer loaded successfully: %s", model_id)
            except asyncio.CancelledError:
                logging.info("Primer load cancelled")
                self._state = "unloaded"
                self._error = None
                raise
            except Exception as exc:
                logging.exception("Primer failed to load")
                self._state = "error"
                self._error = str(exc)
                self.tokenizer = None
                self.engine = None
            finally:
                self._ready_event.set()

    def _build_engine(self, model_id: str) -> AsyncLLMEngine:
        engine_args = AsyncEngineArgs(
            model=model_id,
            trust_remote_code=True,
            gpu_memory_utilization=self.gpu_memory_utilization,
            tensor_parallel_size=self.tensor_parallel_size,
            max_model_len=self.max_model_len,
            disable_log_stats=True,
        )
        return AsyncLLMEngine.from_engine_args(engine_args)

    def _unload(self) -> None:
        self.engine = None
        self.tokenizer = None
        self._state = "unloaded"
        self._error = None
        self._ready_event.clear()
        self._load_task = None

    async def send_work_to_thread(
        self,
        *,
        messages: list[dict[str, Any]],
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stream: bool | None = None,
        grammar: str | None = None,
    ):

        if self.engine is None:
            raise RuntimeError("Primer engine is not initialized")
        if self.tokenizer is None:
            raise RuntimeError("Primer tokenizer is not initialized")

        max_new_tokens = max_new_tokens or self.max_new_tokens
        temperature = self.temperature if temperature is None else temperature
        top_p = self.top_p if top_p is None else top_p

        prompt = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        sampling_params = SamplingParams(
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_new_tokens,
        )

        request_id = f"primer-{uuid.uuid4()}"

        return self.engine.generate(
            prompt,
            sampling_params,
            request_id,
        )

    async def chat_text(
        self,
        *,
        messages: list[dict[str, Any]],
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        grammar: str | None = None,
    ) -> str:
        text = ""
        async for chunk in self.stream_text(
            messages=messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
        ):
            text = chunk
        return text.strip()

    async def chat_json(
        self,
        *,
        messages: list[dict[str, Any]],
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        grammar: str | None = None,
    ) -> dict[str, Any]:
        text = await self.chat_text(
            messages=messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
        )
        return self._parse_json(text)

    async def stream_text(
        self,
        *,
        messages: list[dict[str, Any]],
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
    ) -> AsyncIterator[str]:
        await (
            self.ensure_ready()
        )  # <--- this changes internal variables, should not be here

        if self.engine is None:
            raise RuntimeError("Primer engine is not initialized")
        if self.tokenizer is None:
            raise RuntimeError("Primer tokenizer is not initialized")

        max_new_tokens = max_new_tokens or self.max_new_tokens
        temperature = self.temperature if temperature is None else temperature
        top_p = self.top_p if top_p is None else top_p

        prompt = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        sampling_params = SamplingParams(
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_new_tokens,
        )

        request_id = f"primer-{uuid.uuid4()}"

        results_generator = self.engine.generate(
            prompt,
            sampling_params,
            request_id,
        )

        async for request_output in results_generator:
            if not request_output.outputs:
                continue
            yield request_output.outputs[0].text

    def _parse_json(self, raw: str) -> dict[str, Any]:
        text = raw.strip()

        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            if lines and lines[0].strip().lower() == "json":
                lines = lines[1:]
            text = "\n".join(lines).strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise

    def count_tokens(self, messages: list[dict]) -> int:
        return 0
