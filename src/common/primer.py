from __future__ import annotations

import asyncio
from typing import Any

from common.engine.gguf import GGUFPrimerBackend
from common.engine.vllm import VLLMPrimerBackend
from common.hf_downloader import HFDownloader
from common.protocol.adapter.openai_adapter import OpenAIStyleMessageAdapter
from common.protocol.unified_types import RuntimeMessage
from common.types import MAX_TOKENS_POLICY, SAFETY_TOKEN_SIZE


class Primer:
    def __init__(self) -> None:
        self._backend = None
        self.backend_type = None
        self._message_adapter = None
        self.message_adapter_type = None
        self.model_provider_type = None
        self._model_provider = None
        self._generation_lock = asyncio.Lock()

    async def load_model_provider(self, model_provider_type: str) -> None:
        if self.model_provider_type == model_provider_type and self._model_provider:
            return

        if model_provider_type == "huggingface":
            self._model_provider = HFDownloader()
        else:
            raise ValueError(f"Unsupported model provider = {model_provider_type}")

        self.model_provider_type = model_provider_type

    async def load_backend(self, backend_type: str) -> None:
        if self.backend_type == backend_type and self._backend is not None:
            return

        await self.stop()

        if backend_type == "gguf":
            self._backend = GGUFPrimerBackend()
        elif backend_type == "vllm":
            self._backend = VLLMPrimerBackend()
        else:
            raise ValueError(f"Unsupported backend={backend_type}")

        self.backend_type = backend_type

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
        if self._backend is None:
            return False

        if self._message_adapter is None:
            return False

        return self._backend.is_ready()

    def is_loading(self) -> bool:
        if self._backend is None:
            raise ValueError("Backend is not assigned")
        return self._backend.is_loading()

    def status(self) -> dict[str, Any]:
        if self._backend is None:
            return {"error": "Backend not set up"}
        data = self._backend.status()
        data["backend"] = self.backend_type
        return data

    def get_model(self) -> str:
        if self._backend is None:
            raise ValueError("Backend is not assigned")
        return self._backend.model_id

    def count_tokens(self, messages: list[RuntimeMessage]) -> int:
        if self._backend is None:
            raise ValueError("Backend is not assigned")
        if self._message_adapter is None:
            raise ValueError("Message Adapter not set")

        rendered_messages = self._message_adapter.render_messages(messages=messages)

        return self._backend.count_tokens(messages=rendered_messages)

    async def start_background(self) -> None:
        if self._backend is None:
            raise ValueError("Backend is not assigned")
        await self._backend.start_background()

    async def ensure_ready(self) -> None:
        if self._backend is None:
            raise ValueError("Backend is not assigned")
        await self._backend.ensure_ready()

    async def stop(self) -> None:
        if self._backend is None:
            return
        await self._backend.stop()

    async def load_model(self, *args: Any, **kwargs: Any) -> None:
        if self._backend is None:
            raise ValueError("Backend is not assigned")

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

        await self.load_message_adapter("openai", nothink=True)
        await self._backend.load_model(*args, **kwargs)

    async def load_model_stream(self, *args: Any, **kwargs: Any):
        if self._backend is None:
            yield "error: Backend is not assigned\n"
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

        yield "Loading model into backend...\n"
        await self._backend.load_model(*args, **kwargs)

        yield "Backend model ready.\n"

    def send_work_to_thread(self, *args: Any, **kwargs: Any):
        if self._backend is None:
            raise ValueError("Backend is not assigned")

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

        return self._backend.send_work_to_thread(
            *args,
            messages=rendered_messages,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            stream=stream,
            grammar=grammar,
        )

    async def chat_text(self, **kwargs: Any) -> list[RuntimeMessage]:
        if self._backend is None:
            raise ValueError("Backend is not assigned")
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
            content = await self._backend.chat_text(
                messages=rendered_messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                grammar=grammar,
            )

        return self._message_adapter.parse_response(content=content, role="assistant")

    async def chat_json(self, **kwargs: Any) -> list[RuntimeMessage]:
        if self._backend is None:
            raise ValueError("Backend is not assigned")
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
            content = await self._backend.chat_json(
                messages=rendered_messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                grammar=grammar,
            )

        return self._message_adapter.parse_response(content=content, role="assistant")

    async def stream_text(self, **kwargs: Any):
        if self._backend is None:
            raise ValueError("Backend is not assigned")
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
            async for chunk in self._backend.stream_text(
                messages=rendered_messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
            ):
                yield chunk
