from __future__ import annotations

import asyncio
import httpx
from typing import Any

from common.engine.gguf import GGUFPrimerEngine
from common.engine.vllm import VLLMPrimerEngine
from common.hf_downloader import HFDownloader
from common.protocol.adapter.openai_adapter import OpenAIStyleMessageAdapter
from common.protocol.unified_types import RuntimeMessage
from common.types import MAX_TOKENS_POLICY, SAFETY_TOKEN_SIZE
from common.types import Profile
from common.system import profiler

class Primer:
    def __init__(self, external_http: httpx.AsyncClient) -> None:
        self._engine = None
        self.engine_type = None
        self._message_adapter = None
        self.message_adapter_type = None
        self.model_provider_type = None
        self._model_provider = None
        self._external_http_client = external_http
        self._profile = Profile()
        self._generation_lock = asyncio.Lock()

        self.load_profile()
    
    def load_profile(self):
        self._profile.set_machine_info(info=profiler.get_linux_info())

    async def load_model_provider(self, model_provider_type: str) -> None:
        if self.model_provider_type == model_provider_type and self._model_provider:
            return

        if model_provider_type == "huggingface":
            self._model_provider = HFDownloader(external_http=self._external_http_client)
        else:
            raise ValueError(f"Unsupported model provider = {model_provider_type}")

        self.model_provider_type = model_provider_type

    async def load_engine(self, engine_type: str) -> None:
        if self.engine_type == engine_type and self._engine is not None:
            return

        await self.stop()

        if engine_type == "gguf":
            self._engine = GGUFPrimerEngine()
        elif engine_type == "vllm":
            self._engine = VLLMPrimerEngine()
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

    # async def start_background(self) -> None:
    #     if self._engine is None:
    #         raise ValueError("engine is not assigned")
    #     await self._engine.start_background()

    # async def ensure_ready(self) -> None:
    #     if self._engine is None:
    #         raise ValueError("engine is not assigned")
    #     await self._engine.ensure_ready()

    async def stop(self) -> None:
        if self._engine is None:
            return
        await self._engine.stop()

    async def load_model(self, *args: Any, **kwargs: Any) -> None:
        if self._engine is None:
            raise ValueError("engine is not assigned")

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

        if kwargs['path']:
            profiles = profiler.get_model_profile(
                    path=kwargs["path"],
                    reserve_size=self._profile.reserve_size, 
                    safety_size=self._profile.safety_size, 
                    profile_factors=self._profile.runtime_profiles
                )
            self._profile.set_profiles(profiles=profiles)
            kwargs['current_profile'] = self._profile.get_current_profile()

        await self.load_message_adapter("openai", nothink=True)
        await self._engine.load_model(*args, **kwargs)

    async def load_model_stream(self, *args: Any, **kwargs: Any):
        if self._engine is None:
            yield "error: engine is not assigned\n"
            return

        repo_id = kwargs.get("repo_id")
        filename = kwargs.get("filename")
        revision = kwargs.get("revision", "main")
        force_reload = kwargs.get("force_reload", False)

        provider = kwargs.pop("provider", "")
        await self.load_model_provider(model_provider_type=provider)

        if self._model_provider and repo_id and filename:
            exists, path = self._model_provider.exists(
                repo_id=repo_id,
                filename=filename,
            )

            if not exists or force_reload:
                yield f"Model file not found locally. Downloading {repo_id}/{filename}...\n"

                async for progress in self._model_provider.download_file_stream(
                    repo_id=repo_id,
                    filename=filename,
                    revision=revision,
                    force=force_reload,
                ):
                    yield progress

                yield "\nDownload complete.\n"
            else:
                yield f"Model file found locally: {path}\n"

            kwargs["path"] = str(path)

            kwargs.pop("repo_id", None)
            kwargs.pop("filename", None)
            kwargs.pop("revision", None)

        yield "Loading message adapter...\n"
        await self.load_message_adapter("openai", nothink=True)

        yield "Loading model into engine...\n"
        await self._engine.load_model(*args, **kwargs)

        yield "engine model ready.\n"

    def send_work_to_thread(self, *args: Any, **kwargs: Any):
        if self._engine is None:
            raise ValueError("engine is not assigned")

        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
        temperature = 0.9
        stream = False
        grammar = None
        if constraints:
            temperature = constraints.get("temperature", 0.9)
            stream = constraints.get("stream", False)
            grammar = constraints.get("grammar", None)

        messages = kwargs.get("messages", [])
        if not messages:
            raise ValueError("Something is wrong with messages")

        rendered_messages = self._message_adapter.render_messages(
            messages=messages, operation=operation, constraints=constraints
        )

        return self._engine.send_work_to_thread(
            *args,
            messages=rendered_messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stream=stream,
            grammar=grammar,
        )

    async def chat_text(self, **kwargs: Any) -> list[RuntimeMessage]:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
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

        async with self._generation_lock:
            content = await self._engine.chat_text(
                messages=rendered_messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                grammar=grammar,
            )

        return self._message_adapter.parse_response(content=content, role="assistant")

    async def chat_json(self, **kwargs: Any) -> list[RuntimeMessage]:
        if self._engine is None:
            raise ValueError("engine is not assigned")
        if self._message_adapter is None:
            raise ValueError("No adapter set")

        constraints = kwargs.get("constraints", {})
        operation = kwargs.get("operation", "chat")
        max_new_tokens = MAX_TOKENS_POLICY.get(operation, 128) + SAFETY_TOKEN_SIZE
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
        async with self._generation_lock:
            content = await self._engine.chat_json(
                messages=rendered_messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                grammar=grammar,
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
            ):
                yield chunk
