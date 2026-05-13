import logging
from typing import Any

from common.protocol.memory_types import (
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


class MemoryRuntime:
    def __init__(self) -> None:
        self._memoryDB = MemoryDB(DB_PATH)
        self._filememory = FileMemory(self._memoryDB)

    async def handle_ingress(self, req: WorkPacket) -> WorkResult:
        op = req.task.operation
        memory_request = req.task.memory_request
        inputs = req.task.inputs

        if not memory_request:
            raise ValueError("Memory Request is not set")

        try:
            match op:
                case "create_summary":
                    if not isinstance(memory_request.request, Summary):
                        raise ValueError("request is not of Summary instance")

                    summary_req = memory_request.request
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
                    if not isinstance(memory_request.request, Note):
                        raise ValueError("request is not of Note instance")

                    note_request = memory_request.request

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
                    if not isinstance(memory_request.request, MemoryEvent):
                        raise ValueError("request is not of MemoryEvent instance")

                    memory_event_request = memory_request.request

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
                    if not isinstance(memory_request.request, MemoryClaim):
                        raise ValueError("request is not of MemoryClaim instance")

                    memory_claim_request = memory_request.request

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
                    if not isinstance(memory_request.request, ListMemoryClaim):
                        raise ValueError("request is not of ListMemoryClaim instance")

                    list_memory_claim_request = memory_request.request

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
                    if not isinstance(memory_request.request, RetrievalRequest):
                        raise ValueError("request is not of RetrievalRequest instance")

                    retrieval_request = memory_request.request

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
                    if not isinstance(memory_request.request, PromptContextRequest):
                        raise ValueError(
                            "request is not of PromptContextRequest instance"
                        )

                    memory_prompt_request = memory_request.request

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
                    if not isinstance(memory_request.request, User):
                        raise ValueError("request is not of User instance")

                    memory_user_request = memory_request.request

                    user = self._memoryDB.upsert_user(
                        external_id=memory_user_request.external_id,
                        display_name=memory_user_request.display_name,
                        memory_policy=memory_user_request.memory_policy,
                    )
                    return self._ok(req=req, user=user)
                case "create_session":
                    if not isinstance(memory_request.request, Session):
                        raise ValueError("request is not of Session instance")

                    memory_session_request = memory_request.request

                    session = self._memoryDB.create_chat_session(
                        user_id=memory_session_request.user_id,
                        channel=memory_request.channel,
                        status=memory_session_request.status,
                    )
                    return self._ok(req=req, session=session)

                case "resolve_session":
                    if not isinstance(memory_request.request, ResolveSessionRequest):
                        raise ValueError(
                            "request is not of ResolveSessionRequest instance"
                        )

                    memory_resolve_session = memory_request.request

                    session = self._memoryDB.resolve_or_create_session(
                        user_id=memory_request.user_id,
                        channel=memory_request.channel,
                        max_idle_minutes=memory_resolve_session.max_idle_minutes,
                    )
                    return self._ok(req=req, session=session)

                case "create_message":
                    if not isinstance(memory_request.request, RuntimeMessage):
                        raise ValueError("request is not of RuntimeMessage instance")

                    memory_message_request = memory_request.request

                    metadata_response = []

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
