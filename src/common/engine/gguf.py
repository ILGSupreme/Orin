from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any, Literal

from llama_cpp import Llama, LlamaGrammar
from transformers import AutoTokenizer
from common.system import configuration
from common.types import ModelProfile

PrimerState = Literal["unloaded", "loading", "ready", "error"]


class GGUFPrimerEngine:
    def __init__(self) -> None:

        cfg = configuration.get_configuration("cortex").primer

        self.model_id = cfg.model_id
        self.model_path = cfg.model_path
        self.tokenizer_id = cfg.tokenizer_path
        self.max_new_tokens = cfg.max_new_tokens
        self.temperature = cfg.temperature
        self.top_p = cfg.top_p
        self.max_model_len = cfg.max_model_len
        self.n_gpu_layers = cfg.n_gpu_layer
        self.n_threads = cfg.n_threads
        self.n_batch = cfg.n_batch
        self.verbose = cfg.verbose

        self.tokenizer: Any | None = None
        self.engine: Llama | None = None

        self._state: PrimerState = "unloaded"
        self._error: str | None = None
        self._load_lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._load_task: asyncio.Task[None] | None = None
        self.profiles: dict[str, ModelProfile] = {}
        self.current_profile: ModelProfile | None = None

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
            "ready": self.is_ready(),
            "state": self._state,
            "model_id": self.model_id,
            "model_path": self.model_path,
            "tokenizer_id": self.tokenizer_id,
            "error": self._error,
            "n_gpu_layers": self.n_gpu_layers,
            "n_threads": self.n_threads,
            "n_batch": self.n_batch,
            "effective_n_ctx": self.max_model_len,
            "runtime_profile": self.current_profile,
        }

    async def load_model(
        self,
        *,
        path: str,
        model_id: str,
        tokenizer_id: str | None = None,
        force_reload: bool = False,
        profile: ModelProfile | None = None,
    ) -> None:
        if profile is None:
            raise ValueError("ModelProfile is required to load a GGUF model")

        async with self._load_lock:
            same_model = (
                self._state == "ready"
                and self.engine is not None
                and self.model_path == path
                and self.model_id == model_id
                and self.tokenizer_id == tokenizer_id
                and self.n_gpu_layers == profile.inputs.n_gpu_layers
                and self.n_batch == profile.n_batch
                and self.max_model_len == profile.recommended_n_ctx
            )

            if same_model and not force_reload:
                logging.info("Model already loaded: %s", model_id)
                return

            logging.info("Loading model: %s from %s", model_id, path)

            self._state = "loading"
            self._error = None
            self._ready_event.clear()

            # After this point, the old model is gone.
            self._unload()

            try:
                tokenizer = None

                if tokenizer_id is not None:
                    logging.info("Loading tokenizer: %s", tokenizer_id)
                    tokenizer = await asyncio.to_thread(
                        AutoTokenizer.from_pretrained,
                        tokenizer_id,
                        trust_remote_code=True,
                    )

                logging.info(
                    "Building GGUF engine: path=%s n_gpu_layers=%s n_batch=%s n_ctx=%s",
                    path,
                    profile.inputs.n_gpu_layers,
                    profile.n_batch,
                    profile.recommended_n_ctx,
                )

                engine = await asyncio.to_thread(
                    self._build_engine_with_profile,
                    path,
                    profile.inputs.n_gpu_layers,
                    profile.n_batch,
                    profile.recommended_n_ctx,
                )

            except Exception as exc:
                self.engine = None
                self.tokenizer = None
                self.model_path = None
                self.model_id = None
                self.tokenizer_id = None
                self.current_profile = None

                self._state = "error"
                self._error = str(exc)
                self._ready_event.clear()

                logging.exception("Failed to load model: %s", model_id)
                raise RuntimeError(f"Failed to load model {model_id}: {exc}") from exc

            self.engine = engine
            self.tokenizer = tokenizer

            self.model_path = path
            self.model_id = model_id
            self.tokenizer_id = tokenizer_id
            self.current_profile = profile

            self.n_gpu_layers = profile.inputs.n_gpu_layers
            self.n_batch = profile.n_batch
            self.max_model_len = profile.recommended_n_ctx

            self._state = "ready"
            self._error = None
            self._ready_event.set()

            logging.info(
                "Model loaded: %s n_gpu_layers=%s n_batch=%s n_ctx=%s",
                model_id,
                self.n_gpu_layers,
                self.n_batch,
                self.max_model_len,
            )

    async def stop(self) -> None:
        async with self._load_lock:
            self._unload()

    def _build_engine_with_profile(
        self, model_path: str, n_gpu_layers: int, n_batch: int, n_ctx: int
    ):
        return Llama(
            model_path=model_path,
            n_gpu_layers=n_gpu_layers,
            n_batch=n_batch,
            n_ctx=n_ctx,
            n_threads=self.n_threads,
            verbose=self.verbose,
        )

    def _unload(self) -> None:
        self.model_path = None
        self.model_id = None
        self.tokenizer_id = None
        self.current_profile = None
        self.engine = None
        self.tokenizer = None
        self._state = "unloaded"
        self._error = None
        self._ready_event.clear()
        self._load_task = None

    def _run_full_generation(
        self,
        messages: list[dict[str, Any]],
        max_new_tokens: int,
        temperature: float,
        top_p: float,
        grammar: LlamaGrammar | None = None,
    ) -> str:
        assert self.engine is not None

        completion = self.engine.create_chat_completion(
            messages=messages,
            max_tokens=max_new_tokens,
            temperature=temperature,
            top_p=top_p,
            grammar=grammar,
        )

        return completion["choices"][0]["message"]["content"].strip()

    async def chat_text(
        self,
        *,
        messages: list[dict[str, Any]],
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        grammar: str | LlamaGrammar |  None = None,
    ) -> str:

        grammar_llama = None
        if grammar and isinstance(grammar,str):
            grammar_llama = LlamaGrammar.from_string(grammar=grammar)

        if self.engine is None:
            raise RuntimeError("Primer engine is not initialized")

        max_new_tokens = max_new_tokens or self.max_new_tokens
        temperature = self.temperature if temperature is None else temperature
        top_p = self.top_p if top_p is None else top_p

        return await asyncio.to_thread(
            self._run_full_generation,
            messages,
            max_new_tokens,
            temperature,
            top_p,
            grammar_llama,
        )

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
            grammar=grammar,
        )
        return self._parse_json(text)

    def send_work_to_thread(
        self,
        *,
        messages: list[dict[str, Any]],
        max_new_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stream: bool = False,
        grammar: str | None = None,
    ):
        max_new_tokens = max_new_tokens or self.max_new_tokens
        temperature = self.temperature if temperature is None else temperature
        top_p = self.top_p if top_p is None else top_p

        if stream:
            # We will be getting the actual arguments for starting the stream later
            return {
                "messages": messages,
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
                "top_p": top_p,
                "grammar": grammar,
            }
        else:
            llama_grammar = None
            if grammar:
                llama_grammar = LlamaGrammar.from_string(grammar=grammar)
            return asyncio.create_task(
                self.chat_text(
                    messages=messages,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    grammar=llama_grammar,
                ),
            )

    async def stream_text(
        self,
        *,
        messages: list[dict[str, Any]],
        max_new_tokens: int | None = None,
        temperature: float = 0.1,
        top_p: float = 0.95,
    ):

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def worker():
            try:
                stream = self.engine.create_chat_completion(
                    messages=messages,
                    max_tokens=max_new_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    stream=True,
                )

                for chunk in stream:
                    delta = self._extract_delta(chunk=chunk)
                    if delta:
                        loop.call_soon_threadsafe(queue.put_nowait, delta)

            except Exception as e:
                loop.call_soon_threadsafe(queue.put_nowait, f"\n[stream error: {e}]\n")

            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            delta = await queue.get()

            if delta is None:
                break

            yield delta

    def _extract_delta(self, chunk: dict[str, Any]) -> str:
        try:
            choices = chunk.get("choices", [])
            if not choices:
                return ""
            choice = choices[0]

            delta = choice.get("delta")
            if isinstance(delta, dict):
                return delta.get("content", "") or ""

            message = choice.get("message")
            if isinstance(message, dict):
                return message.get("content", "") or ""

            text = choice.get("text")
            if isinstance(text, str):
                return text

            return ""
        except Exception:
            return ""

    def count_tokens(self, messages: list[dict]) -> int:
        if not self.tokenizer:
            return 0
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        return len(self.tokenizer.encode(text))

    def _parse_json(self, raw: str) -> dict[str, Any]:
        """Parse the raw output from grammar-constrained generation."""
        logging.info(f"JSON RAW:{raw}")
        text = raw.strip()

        # Extremely rare safety net (grammar is very reliable)
        if text.startswith("```"):
            # Remove any lingering markdown that might sneak through
            lines = text.splitlines()
            if lines[0].strip().lower() in {"```json", "```"}:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Direct parse — this is now the happy path
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # Only happens in very rare edge cases (e.g. model truncation)
            logging.warning("Grammar failed to produce perfect JSON: %s", e)
            # Last-resort extraction of the first complete JSON object
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            raise  # Let it fail loudly so you notice
