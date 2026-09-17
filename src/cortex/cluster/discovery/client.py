from __future__ import annotations

from typing import Any

import httpx

from common.protocol.routing_types import WorkPacket


class BackendClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    def _backend_value(self, backend, key: str, default=None):
        if isinstance(backend, dict):
            return backend.get(key, default)

        return getattr(backend, key, default)

    def _backend_name(self, backend) -> str | None:
        return self._backend_value(backend, "name", None)

    async def submit_packet(
        self,
        *,
        backend,
        packet: WorkPacket,
    ) -> dict[str, Any]:
        url = self._build_packet_url(backend)

        response = await self.http.post(url, json=packet.model_dump())
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise TypeError(
                f"Backend {self._backend_name(backend)} returned non-object response"
            )

        return data

    async def retrieve_work(self, *, backend, work_id) -> dict[str, Any]:
        url = self._build_packet_url(backend).rstrip("/") + f"/{work_id}"

        response = await self.http.get(url)
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise TypeError(
                f"Backend {self._backend_name(backend)} returned non-object response"
            )

        return data

    def _build_packet_url(self, backend) -> str:
        base_url = str(self._backend_value(backend, "url", "") or "").rstrip("/")
        path = self._backend_value(backend, "work_path", None) or "/"

        if not base_url:
            raise ValueError(f"Backend {self._backend_name(backend)} is missing url")

        if not base_url.startswith(("http://", "https://")):
            raise ValueError(
                f"Backend {self._backend_name(backend)} has invalid url: {base_url}"
            )

        if not path.startswith("/"):
            path = f"/{path}"

        return f"{base_url}{path}"

    async def get_json(
        self,
        *,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        #async with httpx.AsyncClient(timeout=self.timeout) as client:
        response = await self.http.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise TypeError(f"GET {url} returned non-object response")

        return data

    async def post_json(
        self,
        *,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        #async with httpx.AsyncClient(timeout=self.timeout) as client:
        response = await self.http.post(url, json=payload or {})
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, dict):
            raise TypeError(f"POST {url} returned non-object response")

        return data
