import logging
from typing import Any, TypeVar

from pydantic import BaseModel

from common.protocol.memory_types import (
    BaseRuntimeMemoryRequest,
    ListMemoryClaim,
    MemoryClaim,
    MemoryEvent,
    Note,
    PromptContextRequest,
    ResolveSessionRequest,
    RetrievalRequest,
    Session,
    Summary,
    User,
)
from common.protocol.routing_types import (
    WorkPacket,
    WorkResult,
)
from common.protocol.unified_types import RuntimeMessage
from memory.sql import DB_PATH, NOTES_DIR, SUMMARIES_DIR, FileMemory, MemoryDB

FEDERATION_RECORD_OPS = {
    "federation_put_record",
    "federation_get_record",
    "federation_list_records",
    "federation_delete_record",
}

TRequest = TypeVar("TRequest", bound=BaseModel)


class MemoryRuntime:
    def __init__(self) -> None:
        self._memoryDB = MemoryDB(DB_PATH)
        self._filememory = FileMemory(self._memoryDB)

    async def handle_ingress(self, req: WorkPacket) -> WorkResult:
        op = req.task.operation
        memory_request = req.task.memory_request
        inputs = req.task.inputs

        logging.info(f"Request: {req}")

        try:
            if op in FEDERATION_RECORD_OPS:
                return self._handle_federation_record_operation(
                    req=req,
                    op=op,
                    inputs=inputs,
                )

            if not memory_request:
                raise ValueError("Memory Request is not set")

            match op:
                case "create_summary":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=Summary)

                    summary_req = _resolved_request
                    summary_id = self._memoryDB.save_summary(
                        user_id=memory_request.user_id,
                        session_id=memory_request.session_id,
                        topic=summary_req.topic,
                        summary=summary_req.summary,
                    )
                    path = self._filememory.write_markdown(
                        SUMMARIES_DIR,
                        memory_request.user_id,
                        summary_req.topic,
                        summary_req.summary,
                    )
                    return self._ok(
                        req=req,
                        summary_id=summary_id,
                        path=str(path),
                    )
                case "list_summaries":
                    summaries = self._memoryDB.list_summaries(
                        user_id=memory_request.user_id
                    )
                    return self._ok(req=req, summaries=summaries)
                case "create_note":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=Note)

                    note_request = _resolved_request

                    path = self._filememory.write_markdown(
                        NOTES_DIR,
                        memory_request.user_id,
                        note_request.title,
                        note_request.content,
                        note_request.tags,
                    )
                    note = self._memoryDB.index_note(
                        user_id=memory_request.user_id,
                        session_id=memory_request.session_id,
                        title=note_request.title,
                        path=str(path),
                        tags=note_request.tags,
                    )
                    return self._ok(
                        req=req,
                        note=note,
                        path=str(path),
                    )
                case "list_notes":
                    notes = self._memoryDB.list_notes(user_id=memory_request.user_id)
                    return self._ok(req=req, notes=notes)
                case "create_memory_event":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=MemoryEvent)

                    memory_event_request = _resolved_request

                    event_id = self._memoryDB.create_memory_event(
                        user_id=memory_request.user_id,
                        session_id=memory_request.session_id,
                        raw_text=memory_event_request.raw_text,
                        source=memory_event_request.source,
                    )
                    return self._ok(req=req, event_id=event_id)
                case "list_memory_events":
                    events = self._memoryDB.list_memory_events(
                        user_id=memory_request.user_id,
                        limit=inputs.get("limit", 50),
                    )
                    return self._ok(req=req, events=events)
                case "create_memory_claim":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=MemoryClaim)

                    memory_claim_request = _resolved_request

                    claim = self._memoryDB.upsert_memory_claim(
                        user_id=memory_request.user_id,
                        session_id=memory_request.session_id,
                        memory_type=memory_claim_request.memory_type,
                        subject=memory_claim_request.subject,
                        predicate=memory_claim_request.predicate,
                        value=memory_claim_request.value,
                        confidence=memory_claim_request.confidence,
                        source=memory_claim_request.source,
                        source_event_id=memory_claim_request.source_event_id,
                    )
                    return self._ok(req=req, claim=claim)
                case "list_memory_claims":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=ListMemoryClaim)

                    list_memory_claim_request = _resolved_request

                    claims = self._memoryDB.list_memory_claims(
                        user_id=memory_request.user_id,
                        subject=list_memory_claim_request.subject,
                        predicate=inputs.get("predicate"),
                        memory_type=inputs.get("memory_type"),
                        status=inputs.get("status", "active"),
                    )
                    return self._ok(req=req, claims=claims)
                case "search_memory_claims":
                    claims = self._memoryDB.search_memory_claims(
                        user_id=memory_request.user_id,
                        text=inputs["query"],
                        limit=inputs.get("limit", 25),
                    )
                    return self._ok(req=req, claims=claims)
                case "retrieve":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=RetrievalRequest)

                    retrieval_request = _resolved_request

                    user_id = memory_request.user_id
                    query = retrieval_request.query
                    limit = retrieval_request.limit

                    result: dict[str, Any] = {
                        "retrieved_chunks": [
                            c.__dict__
                            for c in self._filememory.retrieve(user_id, query, limit)
                        ]
                    }

                    if retrieval_request.include_claims:
                        result["claims"] = self._memoryDB.search_memory_claims(
                            user_id=user_id,
                            text=query,
                            limit=limit,
                        )

                    if retrieval_request.include_summaries:
                        result["summaries"] = self._memoryDB.list_summaries(
                            user_id=user_id
                        )[:5]

                    if (
                        retrieval_request.include_recent_messages
                        and memory_request.session_id
                    ):
                        result["recent_messages"] = (
                            self._memoryDB.list_recent_chat_messages(
                                user_id=user_id,
                                session_id=memory_request.session_id,
                                roles=inputs.get(
                                    "recent_message_roles", ["user", "assistant"]
                                ),
                                limit=inputs.get("recent_message_limit", 6),
                            )
                        )

                    return self._ok(req=req, **result)
                case "prompt_context":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=PromptContextRequest)

                    memory_prompt_request = _resolved_request

                    user_id = memory_request.user_id
                    session_id = memory_request.session_id
                    query = memory_prompt_request.query
                    limit = memory_prompt_request.limit

                    claims = []
                    summaries = []
                    chunks = []
                    recent_messages = []

                    if memory_prompt_request.include_claims and query:
                        claims = self._memoryDB.search_memory_claims(
                            user_id=user_id,
                            text=query,
                            limit=limit,
                        )

                    if memory_prompt_request.include_summaries:
                        summaries = self._memoryDB.list_summaries(user_id=user_id)[:5]

                    if memory_prompt_request.include_chunks and query:
                        chunks = self._filememory.retrieve(user_id, query, limit)

                    if (
                        memory_prompt_request.include_recent_messages
                        and session_id
                        and memory_prompt_request.recent_message_limit
                    ):
                        recent_messages = self._memoryDB.list_recent_chat_messages(
                            user_id=user_id,
                            session_id=session_id,
                            roles=memory_prompt_request.recent_message_roles,
                            limit=memory_prompt_request.recent_message_limit,
                        )

                    prompt_lines: list[str] = []

                    if recent_messages:
                        prompt_lines.append("Recent conversation:")
                        for msg in recent_messages:
                            prompt_lines.append(f"- [{msg['role']}] {msg['content']}")
                        prompt_lines.append("")

                    if claims:
                        prompt_lines.append("Known memory:")
                        for claim in claims[:12]:
                            value = claim["value"]
                            if claim["memory_type"] == "plan" and value is True:
                                value = "planned"
                            prompt_lines.append(
                                f"- [{claim['memory_type']}] {claim['subject']} {claim['predicate']} = {value}"
                            )
                        prompt_lines.append("")

                    if summaries:
                        prompt_lines.append("Recent summaries:")
                        for item in summaries[:3]:
                            prompt_lines.append(f"- {item['topic']}: {item['summary']}")
                        prompt_lines.append("")

                    if chunks:
                        prompt_lines.append("Relevant retrieved notes:")
                        for chunk in chunks:
                            prompt_lines.append(
                                f"- ({chunk.source_type}) {chunk.title}: {chunk.snippet}"
                            )

                    return self._ok(
                        req=req,
                        recent_messages=recent_messages,
                        claims=claims,
                        summaries=summaries,
                        retrieved_chunks=[c.__dict__ for c in chunks],
                        prompt_block="\n".join(prompt_lines).strip(),
                    )
                case "upsert_user":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=User)

                    memory_user_request = _resolved_request

                    user = self._memoryDB.upsert_user(
                        external_id=memory_user_request.external_id,
                        display_name=memory_user_request.display_name,
                        memory_policy=memory_user_request.memory_policy,
                    )
                    return self._ok(req=req, user=user)
                case "create_session":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=Session)

                    memory_session_request = _resolved_request

                    session = self._memoryDB.create_chat_session(
                        user_id=memory_session_request.user_id,
                        channel=memory_request.channel,
                        status=memory_session_request.status,
                    )
                    return self._ok(req=req, session=session)

                case "resolve_session":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=ResolveSessionRequest)

                    memory_resolve_session = _resolved_request

                    session = self._memoryDB.resolve_or_create_session(
                        user_id=memory_request.user_id,
                        channel=memory_request.channel,
                        max_idle_minutes=memory_resolve_session.max_idle_minutes,
                    )
                    return self._ok(req=req, session=session)

                case "create_message":
                    _resolved_request = self._memory_request_as(memory_request=memory_request,model_type=RuntimeMessage)

                    memory_message_request = _resolved_request

                    metadata_response = []

                    if not memory_request.session_id:
                        raise ValueError("Session Id not set")

                    for part in memory_message_request.parts:
                        message = self._memoryDB.create_chat_message(
                            session_id=memory_request.session_id,
                            user_id=memory_request.user_id,
                            role=memory_message_request.role,
                            content=part.data,
                            content_type=part.type,
                            metadata=part.metadata,
                        )
                        metadata_response.append(message)
                    return self._ok(req=req, message=metadata_response)

                case "list_recent_messages":
                    if not memory_request.session_id:
                        raise ValueError("Session Id not set")

                    messages = self._memoryDB.list_recent_chat_messages(
                        user_id=memory_request.user_id,
                        session_id=memory_request.session_id,
                        limit=inputs.get("limit", 12),
                        roles=inputs.get("roles"),
                    )
                    return self._ok(req=req, message=messages)

                case _:
                    return self._fail(req=req, error=f"unknown operation {op}")
        except KeyError as e:
            return self._fail(req=req, error=f"missing required input {e.args[0]}")
            # )
        except Exception as e:
            logging.exception("MemoryRuntime operation failed: %s", op)
            return self._fail(req=req, error=str(e))

    async def handle_egress(self, work_id: str):
        return {"status": True}

    def _handle_federation_record_operation(
        self,
        req: WorkPacket,
        op: str,
        inputs: dict[str, Any],
    ) -> WorkResult:
        match op:
            case "federation_put_record":
                return self._handle_federation_put_record(req=req, inputs=inputs)

            case "federation_get_record":
                return self._handle_federation_get_record(req=req, inputs=inputs)

            case "federation_list_records":
                return self._handle_federation_list_records(req=req, inputs=inputs)

            case "federation_delete_record":
                return self._handle_federation_delete_record(req=req, inputs=inputs)

            case _:
                raise ValueError(f"Unsupported federation record operation: {op}")

    def _require_str_input(
        self,
        inputs: dict[str, Any],
        name: str,
        *aliases: str,
    ) -> str:
        value = inputs.get(name)

        if value is None:
            for alias in aliases:
                value = inputs.get(alias)
                if value is not None:
                    break

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or invalid input: {name}")

        return value.strip()

    def _optional_str_input(
        self,
        inputs: dict[str, Any],
        name: str,
        *aliases: str,
    ) -> str | None:
        value = inputs.get(name)

        if value is None:
            for alias in aliases:
                value = inputs.get(alias)
                if value is not None:
                    break

        if value is None:
            return None

        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Invalid input: {name}")

        return value.strip()

    def _handle_federation_put_record(
        self,
        req: WorkPacket,
        inputs: dict[str, Any],
    ) -> WorkResult:
        record_type = self._require_str_input(inputs, "record_type", "type")
        record_id = self._require_str_input(inputs, "record_id", "id", "key")

        value = inputs.get("value", inputs.get("record"))
        if not isinstance(value, dict):
            raise ValueError("Missing or invalid input: value")

        parent_id = self._optional_str_input(inputs, "parent_id", "network_id")
        slug = self._optional_str_input(inputs, "slug")

        metadata = inputs.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError("Invalid input: metadata")

        record = self._memoryDB.put_federation_record(
            record_type=record_type,
            record_id=record_id,
            value=value,
            parent_id=parent_id,
            slug=slug,
            metadata=metadata,
        )

        return self._ok(
            req=req,
            record=record,
        )

    def _handle_federation_get_record(
        self,
        req: WorkPacket,
        inputs: dict[str, Any],
    ) -> WorkResult:
        record_type = self._require_str_input(inputs, "record_type", "type")
        record_id = self._require_str_input(inputs, "record_id", "id", "key")

        record = self._memoryDB.get_federation_record(
            record_type=record_type,
            record_id=record_id,
        )

        return self._ok(
            req=req,
            found=record is not None,
            record=record,
        )

    def _handle_federation_list_records(
        self,
        req: WorkPacket,
        inputs: dict[str, Any],
    ) -> WorkResult:
        record_type = self._require_str_input(inputs, "record_type", "type")
        parent_id = self._optional_str_input(inputs, "parent_id", "network_id")
        slug = self._optional_str_input(inputs, "slug")

        limit_raw = inputs.get("limit", 100)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise ValueError("Invalid input: limit")

        limit = max(1, min(limit, 500))

        records = self._memoryDB.list_federation_records(
            record_type=record_type,
            parent_id=parent_id,
            slug=slug,
            limit=limit,
        )

        return self._ok(
            req=req,
            records=records,
            count=len(records),
        )

    def _handle_federation_delete_record(
        self,
        req: WorkPacket,
        inputs: dict[str, Any],
    ) -> WorkResult:
        record_type = self._require_str_input(inputs, "record_type", "type")
        record_id = self._require_str_input(inputs, "record_id", "id", "key")

        result = self._memoryDB.delete_federation_record(
            record_type=record_type,
            record_id=record_id,
        )

        return self._ok(
            req=req,
            **result,
        )

    def _memory_request_as(
        self,
        memory_request: BaseRuntimeMemoryRequest,
        model_type: type[TRequest],
    ) -> TRequest:
        return model_type.model_validate(memory_request.request)

    def _ok(self, req: WorkPacket, **metadata: Any) -> WorkResult:
        return WorkResult(
            status="completed",
            work_id=req.work_id,
            metadata=metadata,
        )

    def _fail(self, req: WorkPacket, error: str) -> WorkResult:
        return WorkResult(
            status="failed",
            work_id=req.work_id,
            error=error,
        )
