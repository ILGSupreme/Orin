from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from common.jobs import Job
from common.protocol.adapter.openai_adapter import OpenAIStyleMessageAdapter
from common.protocol.grammar_schema import grammar_schema
from common.protocol.routing_types import WorkResult
from common.protocol.unified_types import RuntimeMessage
from common.provider.huggingface.hf_downloader import HFDownloader
from common.system import configuration
from common.types import MAX_TOKENS_POLICY, SAFETY_TOKEN_SIZE, Profile, GGUFLoadOptions


class Primer:
    def __init__(
        self, external_http: httpx.AsyncClient, cfg: configuration.ServiceFileType
    ) -> None:
        self._engine = None
        self.engine_type = None
        self._message_adapter = None
        self.message_adapter_type = None
        self.model_provider_type = None
        self._model_provider = None
        self._external_http_client = external_http
        self._profile = Profile()
        self._generation_lock = asyncio.Lock()
        self._generation_tasks: set[asyncio.Task] = set()
        self.cfg: configuration.ServiceFileType = cfg


    # ---------------------------------------------------------------------------
    # Public Methods
    # ---------------------------------------------------------------------------

    def load_profile(self):
        from common.system import profiler
        self._profile.set_machine_info(info=profiler.get_linux_info())

    def primer_config(self):
        match self.cfg:
            case "cortex":
                return configuration.get_configuration("cortex").primer
            case "llm":
                return configuration.get_configuration("llm").primer
            case _:
                raise RuntimeError(f"Unsupported Primer config type: {self.cfg}")

    async def load_model_provider(self, model_provider_type: str) -> None:
        if self.model_provider_type == model_provider_type and self._model_provider:
            return

        if model_provider_type == "huggingface":
            self._model_provider = HFDownloader(
                external_http=self._external_http_client
            )
        else:
            raise ValueError(f"Unsupported model provider = {model_provider_type}")

        self.model_provider_type = model_provider_type

    async def load_engine(self, engine_type: str) -> None:
        if self.engine_type == engine_type and self._engine is not None:
            return

        await self.stop()

        self.load_profile()
        if engine_type == "gguf":
            from common.engine.gguf import GGUFPrimerEngine
            self._engine = GGUFPrimerEngine(cfg=self.primer_config())
        elif engine_type == "vllm":
            from common.engine.vllm import VLLMPrimerEngine
            self._engine = VLLMPrimerEngine(cfg=self.primer_config())
        else:
            raise ValueError(f"Unsupported engine={engine_type}")

        self.engine_type = engine_type

    async def load_message_adapter(
        self, message_adapter: str, nothink: bool = False
    ) -> None:
        if (
            self.message_adapter_type == message_adapter
            and self._message_adapter is not None
        ):
            return

        match message_adapter:
            case "openai":
                self._message_adapter = OpenAIStyleMessageAdapter(nothink=nothink)
            case _:
                raise ValueError(f"No adapter found for : {message_adapter}")

    def is_ready(self) -> bool:
        if self._engine is None:
            return False

        if self._message_adapter is None:
            return False

        return self._engine.is_ready()

    def is_loading(self) -> bool:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        return self._engine.is_loading()

    def status(self) -> dict[str, Any]:
        if self._engine is None:
            return {"error": "engine not set up"}
        data = self._engine.status()
        data["engine"] = self.engine_type
        return data

    def get_model(self) -> str:
        if self._engine is None or not self._engine.model_id:
            raise ValueError("engine is not assigned")
        return self._engine.model_id

    def count_tokens(self, messages: list[RuntimeMessage]) -> int:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("Message Adapter not set")

        rendered_messages = self._message_adapter.render_messages(messages=messages)

        return self._engine.count_tokens(messages=rendered_messages)

    async def stop(self) -> None:
        if self._engine is None:
            return
        async with self._generation_lock:
            await self._engine.stop()

    async def load_model(self, *args: Any, **kwargs: Any) -> None:
        if self._engine is None:
            raise ValueError("engine is not assigned")

        override = kwargs.pop("gguf_load", None)
        options = (
            GGUFLoadOptions.model_validate(override)
            if override is not None else self.primer_config().gguf_load
        )
        if override is not None and self.engine_type != "gguf":
            raise ValueError("gguf_load options require the GGUF engine")
        repo_id = kwargs.pop("repo_id", None)
        filename = kwargs.pop("filename", None)
        revision = kwargs.pop("revision", "main")
        force_reload = kwargs.get("force_reload", False)

        provider = kwargs.pop("provider", "")
        await self.load_model_provider(model_provider_type=provider)

        if repo_id and filename:
            if self._model_provider is None:
                raise ValueError("Provider is not assigned")

            path = await self._model_provider.ensure_file(
                repo_id=repo_id,
                filename=filename,
                revision=revision,
                force=force_reload,
            )

            kwargs["path"] = str(path)

        async def replace_model():
            if self.engine_type == "gguf":
                from common.system import profiler
                # Release native allocations before measuring available GPU memory.
                await self._engine.stop()
                self._profile.profiles = {}
                selected = self._profile.current_profile
                profiles = profiler.get_model_profile(
                    path=kwargs["path"],
                    reserve_size=options.reserve_bytes,
                    safety_size=options.safety_margin_bytes,
                    profile_factors={selected: self._profile.runtime_profiles[selected]},
                    n_gpu_layers=options.n_gpu_layers,
                    n_ctx=options.n_ctx,
                )
                self._profile.set_profiles(profiles=profiles)
                kwargs["profile"] = self._profile.get_current_profile()
            await self.load_message_adapter("openai", nothink=True)
            await self._engine.load_model(*args, **kwargs)

        # Keep loading serialized with generation even if the caller is cancelled.
        await self._await_generation(replace_model)

    
    # ---------------------------------------------------------------------------
    # Private Methods
    # ---------------------------------------------------------------------------

    def _get_total_token_estimation(self,
        reserved_output_tokens: int, messages: list[RuntimeMessage]
    ):
        prompt_tokens = self.count_tokens(messages)
        total_tokens = prompt_tokens + reserved_output_tokens + SAFETY_TOKEN_SIZE
        return total_tokens
    
    def _can_fit_request(self,
        effective_n_ctx: int,
        reserved_output_tokens: int,
        messages: list[RuntimeMessage],
    ):
        prompt_tokens = self.count_tokens(messages)
        total_estimated_nr_ctx = prompt_tokens + reserved_output_tokens + SAFETY_TOKEN_SIZE
        return total_estimated_nr_ctx <= effective_n_ctx

    async def _await_generation(self, method, **kwargs):
        # Native inference may continue in a thread after its caller is cancelled.
        # Retain the lock until the underlying generation actually finishes.
        await self._generation_lock.acquire()
        try:
            task = asyncio.create_task(method(**kwargs))
        except BaseException:
            self._generation_lock.release()
            raise
        self._generation_tasks.add(task)
        def completed(done):
            self._generation_tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Observe failures even when the caller has left.
            self._generation_lock.release()
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    # ---------------------------------------------------------------------------
    # Chat/Stream Methods
    # ---------------------------------------------------------------------------

    async def chat_text(self, **kwargs: Any) -> str:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
        if "max_output_tokens" in constraints:
            requested = constraints["max_output_tokens"]
            if type(requested) is not int or requested < 1:
                raise ValueError("max_output_tokens must be a positive integer")
            max_new_tokens = min(requested, self.primer_config().max_new_tokens)
        temperature = 0.9
        grammar = None
        if constraints:
            temperature = constraints.get("temperature", 0.9)
            grammar = constraints.get("grammar", None)

        messages = kwargs.get("messages", [])
        if not messages:
            raise ValueError("Something is wrong with messages")

        rendered_messages = self._message_adapter.render_messages(
            messages=messages, operation=operation, constraints=constraints
        )

        content = await self._await_generation(
            self._engine.chat_text, messages=rendered_messages,
            max_new_tokens=max_new_tokens, temperature=temperature, grammar=grammar,
        )

        return content

    async def chat_text_message(self, **kwargs: Any) -> list[RuntimeMessage]:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
        if "max_output_tokens" in constraints:
            requested = constraints["max_output_tokens"]
            if type(requested) is not int or requested < 1:
                raise ValueError("max_output_tokens must be a positive integer")
            max_new_tokens = min(requested, self.primer_config().max_new_tokens)
        temperature = 0.9
        grammar = None
        if constraints:
            temperature = constraints.get("temperature", 0.9)
            grammar = constraints.get("grammar", None)

        messages = kwargs.get("messages", [])
        if not messages:
            raise ValueError("Something is wrong with messages")

        rendered_messages = self._message_adapter.render_messages(
            messages=messages, operation=operation, constraints=constraints
        )

        content = await self._await_generation(
            self._engine.chat_text, messages=rendered_messages,
            max_new_tokens=max_new_tokens, temperature=temperature, grammar=grammar,
        )

        return self._message_adapter.parse_response(content=content, role="assistant")

    async def chat_json(self, **kwargs: Any) -> dict:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
        if "max_output_tokens" in constraints:
            requested = constraints["max_output_tokens"]
            if type(requested) is not int or requested < 1:
                raise ValueError("max_output_tokens must be a positive integer")
            max_new_tokens = min(requested, self.primer_config().max_new_tokens)
        temperature = 0.9
        grammar = None
        if constraints:
            temperature = constraints.get("temperature", 0.9)
            grammar = constraints.get("grammar", None)

        messages = kwargs.get("messages", [])
        if not messages:
            raise ValueError("Something is wrong with messages")

        rendered_messages = self._message_adapter.render_messages(
            messages=messages, operation=operation, constraints=constraints
        )
        if self.engine_type == "gguf" and constraints.get("json_schema") is not None:
            from llama_cpp import LlamaGrammar

            grammar = LlamaGrammar.from_json_schema(
                json.dumps(grammar_schema(constraints["json_schema"])), verbose=False
            )
        return await self._await_generation(
            self._engine.chat_json, messages=rendered_messages,
            max_new_tokens=max_new_tokens, temperature=temperature, grammar=grammar,
        )

    async def chat_json_message(self, **kwargs: Any) -> list[RuntimeMessage]:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
        if "max_output_tokens" in constraints:
            requested = constraints["max_output_tokens"]
            if type(requested) is not int or requested < 1:
                raise ValueError("max_output_tokens must be a positive integer")
            max_new_tokens = min(requested, self.primer_config().max_new_tokens)
        temperature = 0.9
        grammar = None
        if constraints:
            temperature = constraints.get("temperature", 0.9)
            grammar = constraints.get("grammar", None)

        messages = kwargs.get("messages", [])
        if not messages:
            raise ValueError("Something is wrong with messages")

        rendered_messages = self._message_adapter.render_messages(
            messages=messages, operation=operation, constraints=constraints
        )
        content = await self._await_generation(
            self._engine.chat_json, messages=rendered_messages,
            max_new_tokens=max_new_tokens, temperature=temperature, grammar=grammar,
        )

        return self._message_adapter.parse_response(content=content, role="assistant")

    async def stream_text(self, **kwargs: Any):
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
        if "max_output_tokens" in constraints:
            requested = constraints["max_output_tokens"]
            if type(requested) is not int or requested < 1:
                raise ValueError("max_output_tokens must be a positive integer")
            max_new_tokens = min(requested, self.primer_config().max_new_tokens)
        temperature = 0.9
        grammar = None
        if constraints:
            temperature = constraints.get("temperature", 0.9)
            grammar = constraints.get("grammar", None)

        messages = kwargs.get("messages", [])
        if not messages:
            raise ValueError("Something is wrong with messages")

        if any(isinstance(message, RuntimeMessage) for message in messages):
            rendered_messages = self._message_adapter.render_messages(
                messages=messages, operation=operation, constraints=constraints
            )
        else:
            rendered_messages = messages

        async with self._generation_lock:
            async for chunk in self._engine.stream_text(
                messages=rendered_messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                grammar=grammar,
            ):
                yield chunk


# ---------------------------------------------------------------------------
# Job Execute Functions
# ---------------------------------------------------------------------------


async def execute_load_model(job: Job, primer: Primer):
    arguments = dict(job.spec.payload)

    engine = arguments.pop("engine", None)
    model_id = arguments.get("model_id")

    if engine:
        await primer.load_engine(engine)

    await primer.load_model(**arguments)

    return WorkResult(
        status="completed",
        work_id=job.job_id,
        metadata={
            "model_id": model_id,
            "engine": engine,
            "status": "loaded",
        },
    )
