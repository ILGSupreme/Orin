"""Cortex reasoning through interchangeable inference backends."""

from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass
from uuid import uuid4

from async_timeout import timeout

from common.protocol.routing_types import (
    CanonicalTask,
    RoutingHints,
    WorkPacket,
    WorkType,
)
from common.protocol.unified_types import extract_last_assistant_text
from common.system.configuration import InferenceConfiguration

logger = logging.getLogger(__name__)


class InferenceUnavailable(RuntimeError):
    pass


@dataclass
class InferenceResult:
    text: str
    tokens: int
    backend: str


def strict_schema(model):
    """Require every provider schema property, including explicit nullable fields."""
    schema = model.model_json_schema()

    def visit(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["additionalProperties"] = False
                value["required"] = list(value.get("properties", {}))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    return schema


class InferenceGateway:
    def __init__(self, config: InferenceConfiguration, *, primer, router, http):
        self.config, self.primer, self.router, self.http = config, primer, router, http

    def available_for(self, role, privacy="local_only"):
        return any(
            self._available(self.config.backends[name])
            and (self.config.backends[name].kind != "openai"
                 or privacy == "external_allowed")
            for name in self.config.bindings.get(role, [])
        )

    def status(self):
        bindings = {}
        for role, names in self.config.bindings.items():
            bindings[role] = any(
                self._available(self.config.backends[name]) for name in names
            )
        return {
            "bindings": bindings,
            "ready": bindings.get("interpret", False) and bindings.get("decide", False),
        }

    def _available(self, backend):
        if backend.kind == "primer":
            return self.primer is not None and self.primer.is_ready()
        if backend.kind == "openai":
            return self.config.allow_external and bool(os.getenv(backend.api_key_env))
        if self.router is None:
            return False
        return bool(
            self.router.backend_services.find_candidates(
                role="llm", required_capabilities=["chat"]
            )
        )

    async def generate(
        self,
        role,
        messages,
        *,
        privacy,
        model=None,
        max_output_tokens=512,
        budget=32768,
        operation="analyze",
        charge=None,
        checkpoint=None,
    ):
        errors = []
        modalities = {
            part.type if part.type != "json" else "text"
            for msg in messages
            for part in msg.parts
        }
        for name in self.config.bindings[role]:
            backend = self.config.backends[name]
            if backend.kind == "openai" and (
                privacy != "external_allowed" or not self.config.allow_external
            ):
                errors.append(f"{name}: external inference prohibited")
                continue
            if not modalities <= set(backend.modalities):
                errors.append(f"{name}: unsupported input modality")
                continue
            if not self._available(backend):
                errors.append(f"{name}: unavailable")
                continue
            schema_overhead = (
                len(json.dumps(strict_schema(model)).encode("utf-8"))
                if model and backend.kind == "openai"
                else 0
            )
            # Image cost is an estimate; provider usage is reconciled after the call.
            estimate = (
                sum(
                    1024 if part.type == "image" else len(part.data.encode("utf-8"))
                    for msg in messages
                    for part in msg.parts
                )
                + max_output_tokens
                + 64
                + schema_overhead
            )
            context = backend.context_tokens
            if backend.kind == "primer":
                estimate = self.primer._get_total_token_estimation(
                    messages=messages, reserved_output_tokens=max_output_tokens
                )
                context = self.primer.status().get("effective_n_ctx", context)
            if estimate > budget or estimate > context:
                errors.append(f"{name}: context or token allocation limit")
                continue
            if charge:
                await charge(estimate)
            try:
                async with timeout(backend.timeout_seconds):
                    if backend.kind == "openai":
                        text, actual = await self._provider(
                            backend, messages, model, max_output_tokens
                        )
                    elif backend.kind == "primer":
                        kwargs = {
                            "messages": messages,
                            "operation": operation,
                            "constraints": {
                                "stream": False,
                                "temperature": 0,
                                "max_output_tokens": max_output_tokens,
                            },
                        }
                        if model:
                            kwargs["constraints"]["json_schema"] = strict_schema(model)
                            text = json.dumps(await self.primer.chat_json(**kwargs))
                        else:
                            text = await self.primer.chat_text(**kwargs)
                        actual = estimate
                    else:
                        text = await self._routed(
                            messages, max_output_tokens, estimate, operation, checkpoint
                        )
                        actual = estimate
                if not text.strip():
                    raise InferenceUnavailable("Inference returned no text")
                if model:
                    # Enforce the same contract for every inference backend.
                    model.model_validate_json(text)
            except Exception as exc:
                # Never echo provider error bodies, credentials, or submitted data.
                logger.warning(
                    "Inference failed backend=%s role=%s error_type=%s",
                    name, role, type(exc).__name__,
                )
                errors.append(f"{name}: inference failed")
                continue
            if charge and actual > estimate:
                await charge(actual - estimate)
            return InferenceResult(
                text=text, tokens=max(estimate, actual), backend=name
            )
        raise InferenceUnavailable(
            "; ".join(errors) or "No inference backend configured"
        )

    async def _routed(self, messages, output, estimate, operation, checkpoint):
        routed_messages = list(messages)
        packet = WorkPacket(
            work_id="inference:" + uuid4().hex,
            task=CanonicalTask(
                work_type=WorkType.LLM,
                operation=operation,
                messages=routed_messages,
                constraints={
                    "stream": False,
                    "temperature": 0,
                    "max_output_tokens": output,
                },
                routing_hints=RoutingHints(
                    role=WorkType.LLM,
                    required_capabilities=["chat"],
                    required_modalities=sorted(
                        {
                            p.type if p.type != "json" else "text"
                            for m in messages
                            for p in m.parts
                        }
                    ),
                    runtime_preference=[{"effective_n_ctx": estimate}],
                ),
            ),
        )
        if checkpoint:
            await checkpoint(packet.work_id)
        result = await self.router.send(packet)
        if result.status in {"accepted", "running"}:
            if checkpoint:
                await checkpoint(result.work_id)
            result = await self.router.retrieve_completed_work(
                result.work_id,
                WorkType.LLM,
                operation,
                result.metadata["backend_ref"],
            )
        if result.status != "completed":
            raise InferenceUnavailable("Routed inference failed")
        return extract_last_assistant_text(result.content)

    async def _provider(self, backend, messages, model, output):
        wire = []
        for message in messages:
            blocks = []
            for part in message.parts:
                if part.type in {"text", "json"}:
                    blocks.append({"type": "input_text", "text": part.data})
                elif part.type == "image":
                    image = part.data
                    if part.encoding == "base64":
                        mime = part.mime_type or "image/png"
                        if mime not in {
                            "image/png",
                            "image/jpeg",
                            "image/webp",
                            "image/gif",
                        }:
                            raise ValueError("Unsupported image format")
                        image = f"data:{mime};base64,{image}"
                    if not image.startswith(("https://", "data:image/")):
                        raise ValueError("Unsupported image input")
                    blocks.append({"type": "input_image", "image_url": image})
                else:
                    raise ValueError("Unsupported provider input")
            role = (
                message.role
                if message.role in {"system", "developer", "user", "assistant"}
                else "user"
            )
            if role == "assistant":
                if any(block["type"] != "input_text" for block in blocks):
                    raise ValueError("Assistant history must contain text")
                # Easy-input messages accept assistant content as a string.
                wire.append(
                    {"role": role, "content": "\n".join(b["text"] for b in blocks)}
                )
            else:
                wire.append({"role": role, "content": blocks})
        payload = {
            "model": backend.model,
            "input": wire,
            "store": False,
            "max_output_tokens": output,
        }
        if model:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": model.__name__,
                    "strict": True,
                    "schema": strict_schema(model),
                }
            }
        response = await self.http.post(
            backend.url,
            json=payload,
            headers={"Authorization": "Bearer " + os.environ[backend.api_key_env]},
            timeout=backend.timeout_seconds,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "completed":
            raise InferenceUnavailable("Provider response incomplete")
        blocks = [
            part
            for item in data.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
        ]
        if any(part.get("type") == "refusal" for part in blocks):
            raise InferenceUnavailable("Provider refused request")
        text = "".join(
            part["text"] for part in blocks if part.get("type") == "output_text"
        )
        if not text:
            raise InferenceUnavailable("Provider returned no text")
        return text, int(data.get("usage", {}).get("total_tokens", 0))
