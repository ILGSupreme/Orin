# src/federation/storage.py

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeVar
from uuid import uuid4

import httpx
from pydantic import BaseModel

from federation.models import (
    FederationNetwork,
    FederationWorkRecord,
    JoinToken,
    NetworkMember,
)
from common import time
from federation.settings import FederationSettings, get_settings


RecordType = Literal["network", "join_token", "member", "work"]

TModel = TypeVar("TModel", bound=BaseModel)


class FederationStorageError(RuntimeError):
    pass


def _json_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _member_key(network_id: str, cluster_id: str) -> str:
    return f"{network_id}:{cluster_id}"


def _work_key(network_id: str, work_id: str) -> str:
    return f"{network_id}:{work_id}"


class MemoryFederationStorage:
    """
    Persistent Federation storage adapter.

    Federation does not own a separate SQLite database. Persistent records are
    written through the existing Memory service.

    Expected Memory operations:

      federation_put_record
      federation_get_record
      federation_list_records
      federation_delete_record

    Expected inputs:

      {
        "record_type": "network" | "join_token" | "member" | "work",
        "key": "...",
        "value": {...}          # put only
      }

    Expected Memory result metadata:

      get:
        metadata["record"] = {...} | None

      list:
        metadata["records"] = [{...}, ...]

      put/delete:
        metadata may be empty
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: FederationSettings | None = None,
    ) -> None:
        self.http = http
        self.settings = settings or get_settings()

    async def put_network(self, network: FederationNetwork) -> None:
        await self._put_record(
            record_type="network",
            record_id=network.network_id,
            parent_id=network.owner_cluster_id,
            slug=network.slug,
            value=_json_model(network),
            metadata={"source": "federation"},
        )

    async def get_network(self, network_id: str) -> FederationNetwork | None:
        record = await self._get_record("network", network_id)
        return self._model_or_none(FederationNetwork, record)

    async def get_network_by_slug(self, slug: str) -> FederationNetwork | None:
        records = await self._list_records(
            record_type="network",
            slug=slug,
            limit=1,
        )

        if not records:
            return None

        return FederationNetwork.model_validate(records[0])


    async def list_networks(self) -> list[FederationNetwork]:
        records = await self._list_records("network")
        return [FederationNetwork.model_validate(record) for record in records]

    async def delete_network(self, network_id: str) -> None:
        await self._delete_record("network", network_id)

    async def put_join_token(self, token: JoinToken) -> None:
        await self._put_record(
            record_type="join_token",
            record_id=token.token_id,
            parent_id=token.network_id,
            value=_json_model(token),
            metadata={"source": "federation"},
        )

    async def get_join_token(self, token_id: str) -> JoinToken | None:
        record = await self._get_record("join_token", token_id)
        return self._model_or_none(JoinToken, record)

    async def get_join_token_by_hash(self, token_hash: str) -> JoinToken | None:
        tokens = await self.list_join_tokens()

        for token in tokens:
            if token.token_hash == token_hash:
                return token

        return None

    async def list_join_tokens(
        self,
        network_id: str | None = None,
    ) -> list[JoinToken]:
        records = await self._list_records(
            record_type="join_token",
            parent_id=network_id,
        )
        return [JoinToken.model_validate(record) for record in records]

    async def delete_join_token(self, token_id: str) -> None:
        await self._delete_record("join_token", token_id)

    async def put_member(self, member: NetworkMember) -> None:
        await self._put_record(
            record_type="member",
            record_id=_member_key(member.network_id, member.cluster_id),
            parent_id=member.network_id,
            value=_json_model(member),
            metadata={"source": "federation"},
        )

    async def get_member(
        self,
        network_id: str,
        cluster_id: str,
    ) -> NetworkMember | None:
        record = await self._get_record("member", _member_key(network_id, cluster_id))
        return self._model_or_none(NetworkMember, record)

    async def list_members(
        self,
        network_id: str | None = None,
    ) -> list[NetworkMember]:
        records = await self._list_records(
            record_type="member",
            parent_id=network_id,
        )
        return [NetworkMember.model_validate(record) for record in records]

    async def delete_member(self, network_id: str, cluster_id: str) -> None:
        await self._delete_record("member", _member_key(network_id, cluster_id))

    async def put_work_record(self, record: FederationWorkRecord) -> None:
        await self._put_record(
            record_type="work",
            record_id=_work_key(record.network_id, record.work_id),
            parent_id=record.network_id,
            value=_json_model(record),
            metadata={"source": "federation"},
        )

    async def get_work_record(
        self,
        network_id: str,
        work_id: str,
    ) -> FederationWorkRecord | None:
        record = await self._get_record("work", _work_key(network_id, work_id))
        return self._model_or_none(FederationWorkRecord, record)

    async def list_work_records(
        self,
        network_id: str | None = None,
    ) -> list[FederationWorkRecord]:
        records = await self._list_records(
            record_type="work",
            parent_id=network_id,
        )
        return [FederationWorkRecord.model_validate(record) for record in records]

    async def delete_work_record(self, network_id: str, work_id: str) -> None:
        await self._delete_record("work", _work_key(network_id, work_id))

    async def _put_record(
        self,
        record_type: RecordType,
        record_id: str,
        value: dict[str, Any],
        parent_id: str | None = None,
        slug: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        inputs: dict[str, Any] = {
            "record_type": record_type,
            "record_id": record_id,
            "value": value,
        }

        if parent_id is not None:
            inputs["parent_id"] = parent_id

        if slug is not None:
            inputs["slug"] = slug

        if metadata is not None:
            inputs["metadata"] = metadata

        await self._call_memory(
            operation="federation_put_record",
            inputs=inputs,
        )

    async def _get_record(
        self,
        record_type: RecordType,
        record_id: str,
    ) -> dict[str, Any] | None:
        metadata = await self._call_memory(
            operation="federation_get_record",
            inputs={
                "record_type": record_type,
                "record_id": record_id,
            },
        )

        record = metadata.get("record")

        if record is None:
            return None

        if not isinstance(record, dict):
            raise FederationStorageError(
                f"Memory returned invalid record for {record_type}:{record_id}"
            )

        return self._record_value(record)

    async def _list_records(
        self,
        record_type: RecordType,
        parent_id: str | None = None,
        slug: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        inputs: dict[str, Any] = {
            "record_type": record_type,
            "limit": limit,
        }

        if parent_id is not None:
            inputs["parent_id"] = parent_id

        if slug is not None:
            inputs["slug"] = slug

        metadata = await self._call_memory(
            operation="federation_list_records",
            inputs=inputs,
        )

        records = metadata.get("records", [])

        if not isinstance(records, list):
            raise FederationStorageError(
                f"Memory returned invalid record list for {record_type}"
            )

        values: list[dict[str, Any]] = []

        for record in records:
            if not isinstance(record, dict):
                raise FederationStorageError(
                    f"Memory returned non-object record for {record_type}"
                )

            values.append(self._record_value(record))

        return values

    async def _delete_record(
        self,
        record_type: RecordType,
        record_id: str,
    ) -> None:
        await self._call_memory(
            operation="federation_delete_record",
            inputs={
                "record_type": record_type,
                "record_id": record_id,
            },
        )

    def _record_value(self, record: dict[str, Any]) -> dict[str, Any]:
        value = record.get("value")

        if isinstance(value, dict):
            return value

        return record


    async def _call_memory(
        self,
        operation: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        packet = self._build_memory_packet(operation=operation, inputs=inputs)

        try:
            response = await self.http.post(
                self.settings.memory_work_url(),
                json=packet,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise FederationStorageError(
                f"Memory request failed for operation {operation}: {exc}"
            ) from exc

        try:
            result = response.json()
        except ValueError as exc:
            raise FederationStorageError(
                f"Memory returned invalid JSON for operation {operation}"
            ) from exc

        if not isinstance(result, dict):
            raise FederationStorageError(
                f"Memory returned invalid response for operation {operation}"
            )

        status = result.get("status")

        if status != "completed":
            error = result.get("error") or "unknown memory error"
            raise FederationStorageError(
                f"Memory operation {operation} failed: {error}"
            )

        metadata = result.get("metadata", {})

        if metadata is None:
            return {}

        if not isinstance(metadata, dict):
            raise FederationStorageError(
                f"Memory returned invalid metadata for operation {operation}"
            )

        return metadata

    def _build_memory_packet(
        self,
        operation: str,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "work_id": str(uuid4()),
            "disposition": "direct",
            "task": {
                "work_type": "memory",
                "operation": operation,
                "messages": [],
                "memory_request": None,
                "inputs": inputs,
                "constraints": {},
                "routing_hints": {},
            },
            "metadata": {
                "source": "federation",
            },
        }

    def _model_or_none(
        self,
        model_type: type[TModel],
        record: dict[str, Any] | None,
    ) -> TModel | None:
        if record is None:
            return None

        return model_type.model_validate(record)


class LocalNonceStore:
    """
    Local temporary nonce cache.

    This is intentionally local file state, not persistent Memory state.
    It prevents simple replay within the configured nonce TTL window.
    """

    def __init__(
        self,
        path: Path | None = None,
        ttl_seconds: int | None = None,
        settings: FederationSettings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.path = path or self.settings.data_dir / "cache" / "nonces.json"
        self.ttl_seconds = ttl_seconds or self.settings.nonce_ttl_seconds

    def seen(self, nonce: str) -> bool:
        self.prune()

        nonces = self._read()
        return nonce in nonces

    def remember(self, nonce: str) -> None:
        self.prune()

        nonces = self._read()
        expires_at = time.utcnow() + timedelta(seconds=self.ttl_seconds)

        nonces[nonce] = expires_at.isoformat()
        self._write(nonces)

    def check_and_remember(self, nonce: str) -> bool:
        """
        Returns True if the nonce is new and was stored.
        Returns False if the nonce has already been seen.
        """

        if self.seen(nonce):
            return False

        self.remember(nonce)
        return True

    def prune(self) -> None:
        nonces = self._read()
        now = time.utcnow()

        kept: dict[str, str] = {}

        for nonce, expires_at_raw in nonces.items():
            try:
                expires_at = datetime.fromisoformat(expires_at_raw)
            except ValueError:
                continue

            if expires_at > now:
                kept[nonce] = expires_at_raw

        if kept != nonces:
            self._write(kept)

    def _read(self) -> dict[str, str]:
        if not self.path.is_file():
            return {}

        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(data, dict):
            return {}

        result: dict[str, str] = {}

        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str):
                result[key] = value

        return result

    def _write(self, nonces: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = self.path.with_suffix(".tmp")

        with tmp_path.open("w", encoding="utf-8") as file:
            json.dump(nonces, file, indent=2, sort_keys=True)

        tmp_path.replace(self.path)
