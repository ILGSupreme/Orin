from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any, Literal

from llama_cpp import Llama, LlamaGrammar
from transformers import AutoTokenizer

from common.system.profiler import get_model_profile
from common.types import RESERVE_SIZE, RUNTIMEPROFILES, SAFETY_SIZE, ModelProfile

PrimerState = Literal["unloaded", "loading", "ready", "error"]


class GGUFPrimerBackend:
    def __init__(self) -> None:
        self.model_id = os.getenv("PRIMER_MODEL_ID", "local-gguf")
        self.model_path = os.getenv("PRIMER_MODEL_PATH", "/models/model.gguf")
        self.tokenizer_id = os.getenv("PRIMER_TOKENIZER_ID", "")
        self.max_new_tokens = int(os.getenv("PRIMER_MAX_NEW_TOKENS", "1400"))
        self.temperature = float(os.getenv("PRIMER_TEMPERATURE", "0.1"))
        self.top_p = float(os.getenv("PRIMER_TOP_P", "0.95"))
        self.max_model_len = int(os.getenv("PRIMER_MAX_MODEL_LEN", "4096"))

        self.n_gpu_layers = int(os.getenv("PRIMER_N_GPU_LAYERS", "-1"))
        self.n_threads = int(os.getenv("PRIMER_N_THREADS", "6"))
        self.n_batch = int(os.getenv("PRIMER_N_BATCH", "512"))
        self.verbose = os.getenv("PRIMER_VERBOSE", "true").lower() == "true"

        self.tokenizer: Any | None = None
        self.engine: Llama | None = None

        self._state: PrimerState = "unloaded"
        self._error: str | None = None
        self._load_lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._load_task: asyncio.Task[None] | None = None
        self.profiles: dict[str, ModelProfile] = {}
        self.current_profile: str | None = None

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
            "model_path": self.model_path,
            "tokenizer_id": self.tokenizer_id,
            "error": self._error,
            "n_gpu_layers": self.n_gpu_layers,
            "n_threads": self.n_threads,
            "n_batch": self.n_batch,
            "effective_n_ctx": self.max_model_len,
            "runtime_profile": self.current_profile,
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
        *,
        path: str,
        model_id: str,
        tokenizer_id: str | None = None,
        force_reload: bool = False,
    ) -> None:
        self.profiles = get_model_profile(
            path=path,
            reserve_size=RESERVE_SIZE,
            safety_size=SAFETY_SIZE,
            profile_factors=RUNTIMEPROFILES,
        )

        logging.info("Profiles set to: %s", self.profiles)

        self.model_id = model_id

        await self._load_local_model(
            model_path=path,
            tokenizer_id=tokenizer_id,
            force_reload=force_reload,
        )

    async def _load_local_model(
        self,
        *,
        model_path: str,
        tokenizer_id: str | None = None,
        force_reload: bool = False,
    ) -> None:
        async with self._load_lock:
            same_model = self.model_path == model_path and (
                tokenizer_id is None or self.tokenizer_id == tokenizer_id
            )

            if (
                not force_reload
                and same_model
                and self._state == "ready"
                and self.engine is not None
            ):
                return

            if self._load_task is not None and not self._load_task.done():
                self._load_task.cancel()
                try:
                    await self._load_task
                except asyncio.CancelledError:
                    pass

            self._unload()
            self.model_path = model_path
            if tokenizer_id is not None:
                self.tokenizer_id = tokenizer_id

            self._state = "loading"
            self._error = None
            self._ready_event.clear()

        self._load_task = asyncio.create_task(self._load())
        await self._load_task

    async def stop(self) -> None:
        if self._load_task is not None and not self._load_task.done():
            self._load_task.cancel()
            self.profiles = {}
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

            self._ready_event.clear()

            try:
                tokenizer = None
                if self.tokenizer_id:
                    logging.info("Loading Primer tokenizer: %s", self.tokenizer_id)
                    tokenizer = await asyncio.to_thread(
                        AutoTokenizer.from_pretrained,
                        self.tokenizer_id,
                        trust_remote_code=True,
                    )

                last_exc: Exception | None = None

                for profile_name in ("conservative", "balanced", "aggressive"):
                    value = self.profiles.get(profile_name)
                    logging.info(f"Using Profile : {value}")
                    logging.info("self id: %s", id(self))
                    logging.info("profiles type: %s", type(self.profiles))
                    logging.info("profiles id: %s", id(self.profiles))
                    logging.info("profiles raw: %r", self.profiles)

                    if isinstance(self.profiles, dict):
                        logging.info("profile keys: %s", list(self.profiles.keys()))
                    else:
                        logging.info("self.profiles is not a dict")
                    if value is None:
                        continue
                    try:
                        logging.info(
                            "Loading Primer GGUF engine: %s (n_gpu=%s, n_batch=%s, n_ctx=%s)",
                            self.model_path,
                            value.inputs.n_gpu_layers,
                            value.n_batch,
                            value.recommended_n_ctx,
                        )

                        engine = await asyncio.to_thread(
                            self._build_engine_with_profile,
                            self.model_path,
                            value.inputs.n_gpu_layers,
                            value.n_batch,
                            value.recommended_n_ctx,
                        )

                        self.tokenizer = tokenizer
                        self.engine = engine
                        self.n_gpu_layers = value.inputs.n_gpu_layers
                        self.n_batch = value.n_batch
                        self.max_model_len = value.recommended_n_ctx
                        self._state = "ready"
                        self._error = None
                        self.current_profile = profile_name

                        logging.info(
                            "Primer loaded successfully with profile "
                            "(n_gpu=%s, n_batch=%s, n_ctx=%s)",
                            self.n_gpu_layers,
                            self.n_batch,
                            self.max_model_len,
                        )
                        return

                    except Exception as exc:
                        last_exc = exc
                        logging.warning(
                            "Primer load attempt failed "
                            "(n_gpu=%s, n_batch=%s, n_ctx=%s): %s",
                            value.inputs.n_gpu_layers,
                            value.n_batch,
                            value.recommended_n_ctx,
                            exc,
                        )

                raise RuntimeError(
                    f"All GGUF load attempts failed. Last error: {last_exc}"
                ) from last_exc

            except asyncio.CancelledError:
                logging.info("Primer load cancelled")
                self._state = "unloaded"
                self._error = None
                raise

            except Exception as exc:
                logging.exception("Primer failed to load")
                self._state = "error"
                self._error = str(exc)
                self.engine = None
                self.tokenizer = None

            finally:
                self._ready_event.set()

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
        grammar: str | None = None,
    ) -> str:
        await self.ensure_ready()

        grammar_llama = None
        if grammar:
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
        await self.ensure_ready()

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
