import asyncio
import logging

import httpx

from cortex.cluster.discovery.types import (
    BackendDescriptor,
    BackendHealth,
    BackendHealthStatus,
)

root_logger = logging.getLogger()


class BackendHealthProber:
    def __init__(self, http: httpx.AsyncClient, timeout: float = 2.0) -> None:
        self.http = http
        self.timeout = timeout

    async def probe_all(
        self, backends: list[BackendDescriptor]
    ) -> list[BackendDescriptor]:
        tasks = [self._probe_one(self.http, backend) for backend in backends]
        return await asyncio.gather(*tasks)

    async def _probe_one(
        self,
        client: httpx.AsyncClient,
        backend: BackendDescriptor,
    ) -> BackendDescriptor:
        ready = backend.health.ready_endpoints > 0
        http_ok = False
        model_ok = False

        model_check_attempted = False

        if ready:
            try:
                r = await client.get(f"{backend.url}{backend.health_path}")
                http_ok = 200 <= r.status_code < 300
            except (httpx.RequestError, httpx.HTTPStatusError):
                http_ok = False

            if http_ok and backend.model_status_path:
                model_check_attempted = True
                try:
                    r = await client.get(f"{backend.url}{backend.model_status_path}")

                
                    if 200 <= r.status_code < 300:
                        payload = r.json()
                        root_logger.info(payload)
                        if payload.get("ok"):
                            model = payload.get("model")
                            effective_n_ctx = model.get("effective_n_ctx", 0)
                            backend.runtime.effective_n_ctx = effective_n_ctx
                            model_ok = True
                            
                except (httpx.RequestError, httpx.HTTPStatusError):
                    model_ok = False

        backend.health = BackendHealth(
            registered=True,
            ready_endpoints=backend.health.ready_endpoints,
            http_healthy=http_ok,
            model_healthy=model_ok,
            status=self._compute_status(
                ready_endpoints=backend.health.ready_endpoints,
                http_ok=http_ok,
                model_check_attempted=model_check_attempted,
                model_ok=model_ok,
            ),
        )
        return backend

    @staticmethod
    def _compute_status(
        *,
        ready_endpoints: int,
        http_ok: bool,
        model_check_attempted: bool,
        model_ok: bool,
    ) -> BackendHealthStatus:
        if ready_endpoints <= 0:
            return BackendHealthStatus("unavailable", "No ready endpoints")
        if not http_ok:
            return BackendHealthStatus("degraded", "Health endpoint failed")
        if model_check_attempted and not model_ok:
            return BackendHealthStatus("degraded", "Model endpoint failed")
        return BackendHealthStatus("healthy", "")
