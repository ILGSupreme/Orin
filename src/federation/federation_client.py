# src/federation/federation_client.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from .envelopes import create_envelope, envelope_to_json
from .identity import ClusterIdentity
from .models import (
    CapabilitySummary,
    GetFederatedWorkResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    JoinNetworkResponse,
    PublishCapabilitiesRequest,
    PublishCapabilitiesResponse,
    SubmitFederatedWorkResponse,
)


class FederationClientError(RuntimeError):
    pass


class FederationClientResponseError(FederationClientError):
    pass


class FederationClientHttpError(FederationClientError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FederationClient:
    """
    Outbound client for talking to another Federation node.

    This client must only talk to Federation endpoints. It should not talk
    directly to Cortex, LLM, Memory, Tool, Kubernetes, or backend services.

    Responsibilities:
      - public network discovery
      - token-based join
      - signed heartbeat
      - signed capability publish
      - signed FederatedWorkEnvelope submission
      - result polling through Federation
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        identity: ClusterIdentity,
        protocol_version: str = "v1",
        request_ttl_seconds: int = 300,
    ) -> None:
        self.http = http
        self.identity = identity
        self.protocol_version = protocol_version
        self.request_ttl_seconds = request_ttl_seconds

    async def list_networks(
        self,
        *,
        base_url: str,
    ) -> dict[str, Any]:
        return await self._get_json(
            self._url(base_url, "/federation/networks"),
        )

    async def get_network(
        self,
        *,
        base_url: str,
        slug: str,
    ) -> dict[str, Any]:
        return await self._get_json(
            self._url(base_url, f"/federation/networks/{slug}"),
        )

    async def get_network_capabilities(
        self,
        *,
        base_url: str,
        slug: str,
    ) -> dict[str, Any]:
        return await self._get_json(
            self._url(base_url, f"/federation/networks/{slug}/capabilities"),
        )

    async def join_network(
        self,
        *,
        base_url: str,
        slug: str,
        token: str,
        advertised_capabilities: list[CapabilitySummary] | None = None,
    ) -> JoinNetworkResponse:
        payload = {
            "token": token,
            "cluster_id": self.identity.cluster_id,
            "public_key": self.identity.public_key,
            "advertised_capabilities": [
                capability.model_dump(mode="json")
                for capability in advertised_capabilities or []
            ],
        }

        data = await self._post_json(
            self._url(base_url, f"/federation/networks/{slug}/join"),
            payload,
        )

        return JoinNetworkResponse.model_validate(data)

    async def send_heartbeat(
        self,
        *,
        base_url: str,
        network_id: str,
    ) -> HeartbeatResponse:
        body = self._signed_member_body(
            network_id=network_id,
            fields={
                "cluster_id": self.identity.cluster_id,
            },
        )

        data = await self._post_json(
            self._url(base_url, f"/federation/networks/{network_id}/heartbeat"),
            body,
        )

        return HeartbeatResponse.model_validate(data)

    async def publish_capabilities(
        self,
        *,
        base_url: str,
        network_id: str,
        capabilities: list[CapabilitySummary],
    ) -> PublishCapabilitiesResponse:
        body = self._signed_member_body(
            network_id=network_id,
            fields={
                "cluster_id": self.identity.cluster_id,
                "capabilities": [
                    capability.model_dump(mode="json") for capability in capabilities
                ],
            },
        )

        #request = PublishCapabilitiesRequest.model_validate(body)

        data = await self._post_json(
            self._url(base_url, f"/federation/networks/{network_id}/capabilities"),
            body
        )

        return PublishCapabilitiesResponse.model_validate(data)

    async def submit_work(
        self,
        *,
        base_url: str,
        network_id: str,
        packet: dict[str, Any],
        target_cluster_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SubmitFederatedWorkResponse:
        """
        Submit a WorkPacket-shaped dict to a remote Federation node.

        The packet is wrapped in a signed FederatedWorkEnvelope. The receiving
        Federation node is responsible for membership, signature, policy, nonce,
        and WorkPacket validation before submitting it to its local Cortex.
        """

        envelope = create_envelope(
            identity=self.identity,
            ttl_seconds=self.request_ttl_seconds,
            protocol_version=self.protocol_version,
            network_id=network_id,
            target_cluster_id=target_cluster_id,
            request_id=request_id,
            packet=packet,
            metadata=metadata,
        )

        data = await self._post_json(
            self._url(base_url, f"/federation/networks/{network_id}/work"),
            envelope_to_json(envelope),
        )

        return SubmitFederatedWorkResponse.model_validate(data)

    async def get_work_result(
        self,
        *,
        base_url: str,
        network_id: str,
        work_id: str,
    ) -> GetFederatedWorkResponse:
        data = await self._get_json(
            self._url(base_url, f"/federation/networks/{network_id}/work/{work_id}"),
        )

        return GetFederatedWorkResponse.model_validate(data)

    async def submit_work_and_poll_once(
        self,
        *,
        base_url: str,
        network_id: str,
        packet: dict[str, Any],
        target_cluster_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> GetFederatedWorkResponse:
        """
        Convenience helper for tests.

        Submit work, then fetch the result once. This does not loop or wait.
        Higher-level routing code should own retry/backoff/deadline behavior.
        """

        submitted = await self.submit_work(
            base_url=base_url,
            network_id=network_id,
            packet=packet,
            target_cluster_id=target_cluster_id,
            request_id=request_id,
            metadata=metadata,
        )

        return await self.get_work_result(
            base_url=base_url,
            network_id=network_id,
            work_id=submitted.work_id,
        )

    def _signed_member_body(
        self,
        *,
        network_id: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Create body for simple signed member requests.

        Must match app.py::_verify_signed_request():

          payload = {
              "network_id": network_id,
              "body": body_without_signature,
          }
        """

        body = {
            **fields,
            "issued_at": utc_now().isoformat(),
            "nonce": str(uuid4()),
            "signature": "",
        }

        payload = {
            "network_id": network_id,
            "body": {key: value for key, value in body.items() if key != "signature"},
        }

        body["signature"] = self.identity.sign_json(payload)
        return body

    async def _get_json(
        self,
        url: str,
    ) -> dict[str, Any]:
        try:
            response = await self.http.get(url)
        except httpx.HTTPError as exc:
            raise FederationClientHttpError(
                f"Federation GET failed: {url}: {exc}"
            ) from exc

        return self._handle_response(response, context=f"GET {url}")

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await self.http.post(url, json=payload)
        except httpx.HTTPError as exc:
            raise FederationClientHttpError(
                f"Federation POST failed: {url}: {exc}"
            ) from exc

        return self._handle_response(response, context=f"POST {url}")

    def _handle_response(
        self,
        response: httpx.Response,
        *,
        context: str,
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FederationClientResponseError(
                f"{context} returned invalid JSON with status {response.status_code}"
            ) from exc

        if not isinstance(payload, dict):
            raise FederationClientResponseError(
                f"{context} returned non-object JSON with status {response.status_code}"
            )

        if response.status_code >= 400:
            detail = payload.get("detail") or payload.get("error") or payload
            raise FederationClientHttpError(
                f"{context} failed with status {response.status_code}: {detail}"
            )

        return payload

    def _url(
        self,
        base_url: str,
        path: str,
    ) -> str:
        base = base_url.rstrip("/")

        if not base.startswith(("http://", "https://")):
            raise FederationClientError(
                f"Federation base_url must start with http:// or https://: {base_url}"
            )

        if not path.startswith("/"):
            path = "/" + path

        return f"{base}{path}"
