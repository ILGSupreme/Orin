from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

BASE_DIR = Path(os.environ.get("MEMORY_BASE_DIR", "./cluster-memory"))
DB_PATH = BASE_DIR / "memory.db"
NOTES_DIR = BASE_DIR / "notes"
SUMMARIES_DIR = BASE_DIR / "summaries"
DOCS_DIR = BASE_DIR / "docs"
YAML_DIR = BASE_DIR / "yaml"

STOPWORDS = {
    "what",
    "was",
    "the",
    "of",
    "in",
    "on",
    "do",
    "you",
    "remember",
    "about",
    "my",
    "a",
    "an",
    "is",
    "are",
    "to",
    "this",
    "that",
    "i",
    "me",
    "we",
    "our",
    "it",
    "hmm",
    "cant",
    "can't",
    "recall",
    "please",
    "tell",
}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


WORD_RE = re.compile(r"[a-zA-Z0-9_:\-.]+")


def tokenize(text: str) -> set[str]:
    return {m.group(0).lower() for m in WORD_RE.finditer(text)}


def _tokenize_query(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9_.:-]+", text.lower())
    filtered = [t for t in tokens if len(t) >= 2 and t not in STOPWORDS]

    seen = set()
    result = []
    for t in filtered:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "untitled"


def best_snippet(text: str, tokens: set[str], window: int = 260) -> str:
    lowered = text.lower()
    positions = [lowered.find(t) for t in tokens if lowered.find(t) >= 0]
    if not positions:
        return text[:window].strip()
    start = max(0, min(positions) - 80)
    end = min(len(text), start + window)
    return text[start:end].strip().replace("\n", " ")


# -----------------------------------------------------------------------------
# File-backed notes and retrieval support
# -----------------------------------------------------------------------------


@dataclass
class RetrievedChunk:
    source_type: Literal["note", "summary", "doc", "yaml"]
    path: str
    title: str
    score: int
    snippet: str


class FileMemory:
    SEARCH_DIRS: list[tuple[Literal["note", "summary", "doc", "yaml"], Path]] = [
        ("note", NOTES_DIR),
        ("summary", SUMMARIES_DIR),
        ("doc", DOCS_DIR),
        ("yaml", YAML_DIR),
    ]

    def __init__(self, db: "MemoryDB") -> None:
        self.db = db
        for directory in (NOTES_DIR, SUMMARIES_DIR, DOCS_DIR, YAML_DIR):
            directory.mkdir(parents=True, exist_ok=True)

    def _user_dir(self, base_dir: Path, user_id: str) -> Path:
        path = base_dir / slugify(user_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_markdown(
        self,
        base_dir: Path,
        user_id: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> Path:
        directory = self._user_dir(base_dir, user_id)
        slug = slugify(title)
        path = directory / f"{slug}.md"
        body = f"# {title}\n\n{content.strip()}\n"
        path.write_text(body, encoding="utf-8")

        if base_dir == NOTES_DIR:
            self.db.index_note(
                user_id=user_id,
                title=title,
                path=str(path),
                tags=tags or [],
                session_id=session_id,
            )

        return path

    def write_text_file(
        self, base_dir: Path, user_id: str, filename: str, content: str
    ) -> Path:
        directory = self._user_dir(base_dir, user_id)
        path = directory / filename
        path.write_text(content, encoding="utf-8")
        return path

    def retrieve(
        self, user_id: str, query: str, limit: int = 6
    ) -> list[RetrievedChunk]:
        tokens = tokenize(query)
        results: list[RetrievedChunk] = []

        search_dirs: list[tuple[Literal["note", "summary", "doc", "yaml"], Path]] = [
            ("note", self._user_dir(NOTES_DIR, user_id)),
            ("summary", self._user_dir(SUMMARIES_DIR, user_id)),
            ("doc", self._user_dir(DOCS_DIR, user_id)),
            ("yaml", self._user_dir(YAML_DIR, user_id)),
        ]

        for source_type, directory in search_dirs:
            for path in directory.glob("**/*"):
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue

                text_tokens = tokenize(text)
                score = len(tokens & text_tokens)
                if score == 0:
                    continue

                title = path.stem.replace("_", " ")
                snippet = best_snippet(text, tokens)
                results.append(
                    RetrievedChunk(
                        source_type=source_type,
                        path=str(path),
                        title=title,
                        score=score,
                        snippet=snippet,
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:limit]


# -----------------------------------------------------------------------------
# Database layer
# -----------------------------------------------------------------------------


class MemoryDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._ensure_parent()
        self._init_db()
        self._migrate_db()

    def _ensure_parent(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        YAML_DIR.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    external_id TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    memory_policy TEXT NOT NULL DEFAULT 'explicit_only',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'text',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    topic TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS note_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    title TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    tags TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS memory_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    raw_text TEXT NOT NULL,
                    source TEXT DEFAULT 'user',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS memory_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    memory_type TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    confidence REAL DEFAULT 1.0,
                    source TEXT DEFAULT '',
                    source_event_id INTEGER,
                    supersedes_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id),
                    FOREIGN KEY(source_event_id) REFERENCES memory_events(id),
                    FOREIGN KEY(supersedes_id) REFERENCES memory_claims(id)
                );
                """
            )

    def _migrate_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_claims_active
                ON memory_claims(user_id, subject, predicate)
                WHERE status = 'active';

                CREATE INDEX IF NOT EXISTS idx_memory_claims_user_updated
                ON memory_claims(user_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_memory_events_user_created
                ON memory_events(user_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_active
                ON chat_sessions(user_id, status, last_active_at DESC);

                CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created
                ON chat_messages(session_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_session_summaries_user_updated
                ON session_summaries(user_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_note_index_user_updated
                ON note_index(user_id, updated_at DESC);
                """
            )

    # ------------------------- summaries -------------------------

    def save_summary(
        self, user_id: str, topic: str, summary: str, session_id: str | None = None
    ) -> int:
        now = utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO session_summaries (user_id, session_id, topic, summary, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, session_id, topic, summary, now, now),
            )
            if cur.lastrowid is None:
                raise RuntimeError("Failed to get lastrowid for session summary insert")
            return int(cur.lastrowid)

    def list_summaries(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM session_summaries
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------- notes -------------------------

    def index_note(
        self,
        user_id: str,
        title: str,
        path: str,
        tags: list[str],
        session_id: str | None = None,
    ) -> None:
        now = utc_now()
        tags_json = json.dumps(tags)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO note_index (user_id, session_id, title, path, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path)
                DO UPDATE SET
                    title = excluded.title,
                    tags = excluded.tags,
                    updated_at = excluded.updated_at
                """,
                (user_id, session_id, title, path, tags_json, now, now),
            )

    def list_notes(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM note_index
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT 100
                """,
                (user_id,),
            ).fetchall()

        parsed: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["tags"] = json.loads(item["tags"])
            parsed.append(item)
        return parsed

    # ------------------------- memory events -------------------------

    def create_memory_event(
        self,
        user_id: str,
        raw_text: str,
        source: str = "user",
        session_id: str | None = None,
    ) -> int:
        now = utc_now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO memory_events (user_id, session_id, raw_text, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, session_id, raw_text, source, now),
            )
            if cur.lastrowid is None:
                raise RuntimeError("Failed to get lastrowid for session summary insert")
            return int(cur.lastrowid)

    def list_memory_events(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM memory_events
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------- memory claims -------------------------

    def list_memory_claims(
        self,
        user_id: str,
        subject: str | None = None,
        predicate: str | None = None,
        memory_type: str | None = None,
        status: str | None = "active",
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM memory_claims WHERE user_id = ?"
        params: list[Any] = [user_id]

        if subject:
            query += " AND subject = ?"
            params.append(subject)

        if predicate:
            query += " AND predicate = ?"
            params.append(predicate)

        if memory_type:
            query += " AND memory_type = ?"
            params.append(memory_type)

        if status:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY updated_at DESC"

        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        parsed: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            parsed.append(item)
        return parsed

    def search_memory_claims(
        self, user_id: str, text: str, limit: int = 25
    ) -> list[dict[str, Any]]:
        tokens = _tokenize_query(text)

        with self._connect() as conn:
            rows = []

            if tokens:
                score_parts = []
                where_parts = []
                score_params: list[Any] = []
                where_params: list[Any] = []

                for token in tokens:
                    like = f"%{token}%"

                    score_parts.append("""
                        (CASE WHEN lower(subject) LIKE ? THEN 3 ELSE 0 END) +
                        (CASE WHEN lower(predicate) LIKE ? THEN 2 ELSE 0 END) +
                        (CASE WHEN lower(value_json) LIKE ? THEN 1 ELSE 0 END) +
                        (CASE WHEN lower(memory_type) LIKE ? THEN 1 ELSE 0 END) +
                        (CASE WHEN lower(source) LIKE ? THEN 1 ELSE 0 END)
                    """)
                    score_params.extend([like, like, like, like, like])

                    where_parts.append("""
                        lower(subject) LIKE ? OR
                        lower(predicate) LIKE ? OR
                        lower(value_json) LIKE ? OR
                        lower(memory_type) LIKE ? OR
                        lower(source) LIKE ?
                    """)
                    where_params.extend([like, like, like, like, like])

                score_expr = " + ".join(f"({p})" for p in score_parts)
                where_expr = " OR ".join(f"({p})" for p in where_parts)

                sql = f"""
                    SELECT *, ({score_expr}) AS match_score
                    FROM memory_claims
                    WHERE user_id = ?
                    AND status = 'active'
                    AND ({where_expr})
                    ORDER BY match_score DESC, updated_at DESC
                    LIMIT ?
                """
                params = score_params + [user_id] + where_params + [limit]
                rows = conn.execute(sql, params).fetchall()

            if not rows:
                rows = conn.execute(
                    """
                    SELECT *, 0 AS match_score
                    FROM memory_claims
                    WHERE user_id = ?
                    AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()

        parsed: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["value"] = json.loads(item.pop("value_json"))
            parsed.append(item)

        return parsed

    def upsert_memory_claim(
        self,
        user_id: str,
        memory_type: str,
        subject: str,
        predicate: str,
        value: Any,
        confidence: float = 1.0,
        source: str = "",
        source_event_id: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)

        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT * FROM memory_claims
                WHERE user_id = ? AND subject = ? AND predicate = ? AND status = 'active'
                LIMIT 1
                """,
                (user_id, subject, predicate),
            ).fetchone()

            if existing:
                existing_value_json = existing["value_json"]
                if existing_value_json == value_json:
                    item = dict(existing)
                    item["value"] = json.loads(item.pop("value_json"))
                    return {"status": "unchanged", "claim": item}

                conn.execute(
                    """
                    UPDATE memory_claims
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, existing["id"]),
                )
                supersedes_id = int(existing["id"])
            else:
                supersedes_id = None

            cur = conn.execute(
                """
                INSERT INTO memory_claims (
                    user_id, session_id, memory_type, subject, predicate, value_json, status,
                    confidence, source, source_event_id, supersedes_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    session_id,
                    memory_type,
                    subject,
                    predicate,
                    value_json,
                    confidence,
                    source,
                    source_event_id,
                    supersedes_id,
                    now,
                    now,
                ),
            )
            if cur.lastrowid is None:
                raise RuntimeError("Failed to get lastrowid for session summary insert")
            claim_id = int(cur.lastrowid)

            row = conn.execute(
                "SELECT * FROM memory_claims WHERE id = ?",
                (claim_id,),
            ).fetchone()

        item = dict(row)
        item["value"] = json.loads(item.pop("value_json"))
        return {
            "status": "updated" if supersedes_id else "created",
            "claim": item,
        }

    # ------------------------- sessions -------------------------

    def create_chat_session(
        self,
        user_id: str,
        channel: str,
        status: str = "active",
        session_id: str | None = None,
    ) -> dict[Any, Any]:
        now = utc_now()
        session_id = session_id or str(uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (id, user_id, channel, status, started_at, last_active_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, channel, status, now, now),
            )

            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

        return dict(row)

    def touch_chat_session(self, session_id: str) -> None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_sessions
                SET last_active_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )

    def parse_iso_utc(self, ts: str) -> datetime:
        return datetime.fromisoformat(ts)

    def get_latest_active_session(
        self, user_id: str, channel: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM chat_sessions
                WHERE user_id = ? AND channel = ? AND status = 'active'
                ORDER BY last_active_at DESC
                LIMIT 1
                """,
                (user_id, channel),
            ).fetchone()

        return dict(row) if row else None

    def close_chat_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_sessions
                SET status = 'closed'
                WHERE id = ?
                """,
                (session_id,),
            )

    def resolve_or_create_session(
        self,
        user_id: str,
        channel: str,
        max_idle_minutes: int = 60,
    ) -> dict[str, Any]:
        existing = self.get_latest_active_session(user_id=user_id, channel=channel)
        if not existing:
            return self.create_chat_session(
                user_id=user_id, channel=channel, status="active"
            )

        last_active = datetime.fromisoformat(existing["last_active_at"])
        now = datetime.now(timezone.utc)

        if now - last_active > timedelta(minutes=max_idle_minutes):
            self.close_chat_session(existing["id"])
            return self.create_chat_session(
                user_id=user_id, channel=channel, status="active"
            )

        return existing

    # ------------------------- user -------------------------

    def upsert_user(
        self,
        external_id: str,
        display_name: str | None,
        memory_policy: str,
    ) -> dict[str, Any]:
        now = utc_now()
        user_id = external_id

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, external_id, display_name, memory_policy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    external_id = excluded.external_id,
                    display_name = excluded.display_name,
                    memory_policy = excluded.memory_policy,
                    updated_at = excluded.updated_at
                """,
                (user_id, external_id, display_name, memory_policy, now, now),
            )

            row = conn.execute(
                "SELECT * FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

        return dict(row)

    # ------------------------- Messages -------------------------

    def create_chat_message(
        self,
        session_id: str,
        user_id: str,
        role: str,
        content: str,
        content_type: str = "text",
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> dict[Any, Any]:
        now = utc_now()
        message_id = message_id or str(uuid4())
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_messages (
                    id, session_id, user_id, role, content, content_type, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    user_id,
                    role,
                    content,
                    content_type,
                    metadata_json,
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE chat_sessions
                SET last_active_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )

            row = conn.execute(
                """
                SELECT * FROM chat_messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()

        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def list_recent_chat_messages(
        self,
        user_id: str,
        session_id: str,
        limit: int = 12,
        roles: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            params: list[Any] = [user_id, session_id]
            role_sql = ""

            if roles:
                placeholders = ",".join("?" for _ in roles)
                role_sql = f" AND role IN ({placeholders})"
                params.extend(roles)

            params.append(limit)

            rows = conn.execute(
                f"""
                SELECT *
                FROM chat_messages
                WHERE user_id = ?
                AND session_id = ?
                {role_sql}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        items: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            items.append(item)

        return items
