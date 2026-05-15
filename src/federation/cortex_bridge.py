# src/federation/cortex_bridge.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .models import FederationNetwork, FederationWorkRecord, NetworkMember
from .settings import FederationSettings, get_settings


class CortexBridgeError(RuntimeError):
    pass


class CortexBridgeResponseError(CortexBridgeError):
    pass


class CortexBridgeStatusError(CortexBridgeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CortexBridge:
    """
    Bridge from Federation into local Cortex.

    Federation receives signed external work envelopes. After envelope,
    membership, and policy checks pass, app.py sanitizes the packet and passes
    it here.

    This class expects Cortex to expose an internal WorkPacket-compatible
    endpoint returning a WorkResult-shaped response:

      {
        "status": "accepted" | "running" | "completed" | "failed",
        "work_id": "...",
        "content": [...],
        "backend_name": "...",
        "backend_model": "...",
        "error": null,
        "metadata": {...}
      }

    If Cortex /work currently accepts only InferenceSession, add a Cortex-side
    internal endpoint for WorkPacket submission and point cortex_work_path to it.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: FederationSettings | None = None,
    ) -> None:
        self.http = http
        self.settings = settings or get_settings()

    async def submit_packet(
        self,
        *,
        network: FederationNetwork,
        member: NetworkMember,
        request_id: str,
        packet: dict[str, Any],
        target_cluster_id: str | None = None,
    ) -> FederationWorkRecord:
        work_id = self._ensure_work_id(packet, fallback=request_id)

        result = await self._post_work_packet(packet)

        status = self._extract_status(result)
        result_work_id = self._extract_work_id(result, fallback=work_id)

        now = utc_now()

        record = FederationWorkRecord(
        network_id=network.network_id,
        work_id=result_work_id,
        request_id=request_id,
        origin_cluster_id=member.cluster_id,
        target_cluster_id=target_cluster_id,
        status=status,
        result=result if status in {"completed", "failed"} else None,
        error=self._extract_error(result),
        created_at=now,
        updated_at=now,
        completed_at=now if status in {"completed", "failed"} else None,
        metadata={
            "source": "cortex_bridge",
            "network_slug": network.slug,
            "origin_cluster_id": member.cluster_id,
            "cortex_status": status,
            "cortex_metadata": result.get("metadata", {}),
        },
    )

        return record

    async def poll_result(
        self,
        *,
        network_id: str,
        work_id: str,
        request_id: str,
        origin_cluster_id: str,
        target_cluster_id: str | None = None,
    ) -> FederationWorkRecord:
        result = await self._get_work_result(work_id)

        status = self._extract_status(result)
        result_work_id = self._extract_work_id(result, fallback=work_id)

        now = utc_now()

        return FederationWorkRecord(
            network_id=network_id,
            work_id=result_work_id,
            request_id=request_id,
            origin_cluster_id=origin_cluster_id,
            target_cluster_id=target_cluster_id,
            status=status,
            result=result if status in {"completed", "failed"} else None,
            error=self._extract_error(result),
            updated_at=now,
            completed_at=now if status in {"completed", "failed"} else None,
            metadata={
                "source": "cortex_bridge",
                "cortex_status": status,
                "cortex_metadata": result.get("metadata", {}),
            },
        )

    async def _post_work_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.http.post(
                self.settings.cortex_work_url(),
                json=packet,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CortexBridgeError(f"Cortex work submission failed: {exc}") from exc

        return self._parse_json_response(response, context="Cortex work submission")

    async def _get_work_result(self, work_id: str) -> dict[str, Any]:
        try:
            response = await self.http.get(
                self.settings.cortex_work_result_url(work_id),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise CortexBridgeError(f"Cortex work polling failed: {exc}") from exc

        return self._parse_json_response(response, context="Cortex work polling")

    def _parse_json_response(
        self,
        response: httpx.Response,
        *,
        context: str,
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CortexBridgeResponseError(f"{context} returned invalid JSON") from exc

        if not isinstance(payload, dict):
            raise CortexBridgeResponseError(f"{context} returned non-object JSON")

        return payload

    def _extract_status(self, result: dict[str, Any]) -> str:
        status = result.get("status")

        if isinstance(status, str):
            if status not in {"accepted", "running", "completed", "failed"}:
                raise CortexBridgeStatusError(f"Unsupported Cortex result status: {status}")
            return status

        if result.get("ok") is False:
            error = result.get("error") or "unknown Cortex error"
            raise CortexBridgeStatusError(f"Cortex rejected work: {error}")

        raise CortexBridgeStatusError("Cortex result is missing status")

    def _extract_work_id(
        self,
        result: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        work_id = result.get("work_id")

        if isinstance(work_id, str) and work_id.strip():
            return work_id

        return fallback

    def _extract_error(self, result: dict[str, Any]) -> str | None:
        error = result.get("error")

        if error is None:
            return None

        if isinstance(error, str):
            return error

        return str(error)

    def _ensure_work_id(
        self,
        packet: dict[str, Any],
        *,
        fallback: str,
    ) -> str:
        work_id = packet.get("work_id")

        if isinstance(work_id, str) and work_id.strip():
            return work_id

        packet["work_id"] = fallback
        return fallback