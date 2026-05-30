from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import quote

import httpx


class HFDownloader:
    def __init__(
        self,
        *,
        cache_dir: str = "/models/huggingface",
        external_http: httpx.AsyncClient,
        token: str | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.token = token or os.getenv("HF_TOKEN")
        self.timeout_seconds = timeout_seconds
        self.external_http = external_http

    def _set_idle(self) -> None:
        self._status = "idle"
        self._repo_id = None
        self._filename = None
        self._downloaded_bytes = 0
        self._total_bytes = None
        self._percent = None

    def _build_url(self, repo_id: str, filename: str, revision: str = "main") -> str:
        repo = quote(repo_id, safe="/")
        file_part = quote(filename, safe="/")
        return f"https://huggingface.co/{repo}/resolve/{revision}/{file_part}"

    def _target_path(self, repo_id: str, filename: str) -> Path:
        return self.cache_dir / repo_id / filename

    def exists(self, repo_id: str, filename: str):
        target = self._target_path(repo_id=repo_id, filename=filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return True, target
        else:
            return False, target

    async def download_file_stream(
        self,
        *,
        repo_id: str,
        filename: str,
        revision: str = "main",
        force: bool = False,
    ):
        target = self._target_path(repo_id, filename)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() and not force:
            yield f"cached: {target}\n"
            return

        tmp = target.with_suffix(target.suffix + ".part")
        if tmp.exists():
            tmp.unlink()

        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        url = self._build_url(repo_id, filename, revision=revision)

        downloaded_bytes = 0
        total_bytes: int | None = None
        last_logged_percent = -1

        async with self.external_http.stream("GET", url, headers=headers) as response:
                response.raise_for_status()

                content_length = response.headers.get("Content-Length")
                total_bytes = (
                    int(content_length)
                    if content_length and content_length.isdigit()
                    else None
                )

                with tmp.open("wb") as f:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue

                        f.write(chunk)
                        downloaded_bytes += len(chunk)

                        if total_bytes:
                            percent = round((downloaded_bytes / total_bytes) * 100, 1)
                            current_int = int(percent)

                            if current_int != last_logged_percent:
                                last_logged_percent = current_int
                                yield (
                                    f"Downloading {repo_id}/{filename}: "
                                    f"{percent:.1f}% "
                                    f"({downloaded_bytes}/{total_bytes} bytes)\n"
                                )
                        else:
                            yield (
                                f"Downloading {repo_id}/{filename}: "
                                f"{downloaded_bytes} bytes\n"
                            )

        tmp.replace(target)
        yield f"ready: {target}\n"
        return

    async def ensure_file(
        self,
        *,
        repo_id: str,
        filename: str,
        revision: str = "main",
        force: bool = False,
    ) -> Path:
        target = self._target_path(repo_id, filename)
        logging.debug("TARGET: %s", target)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() and not force:
            logging.info(
                "Using cached file %s/%s: %s bytes",
                repo_id,
                filename,
                target.stat().st_size,
            )
            return target

        tmp = target.with_suffix(target.suffix + ".part")

        if tmp.exists():
            tmp.unlink()

        logging.debug("tmp: %s", tmp)

        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        url = self._build_url(repo_id, filename, revision=revision)

        downloaded_bytes = 0
        total_bytes: int | None = None
        percent: float | None = None
        last_logged_percent = -1

        try:
            async with self.external_http.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()

                    content_length = response.headers.get("Content-Length")
                    total_bytes = (
                        int(content_length)
                        if content_length and content_length.isdigit()
                        else None
                    )

                    with tmp.open("wb") as f:
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue

                            f.write(chunk)
                            downloaded_bytes += len(chunk)

                            if total_bytes and total_bytes > 0:
                                percent = round(
                                    (downloaded_bytes / total_bytes) * 100,
                                    1,
                                )
                                current_int = int(percent)

                                if current_int != last_logged_percent:
                                    last_logged_percent = current_int
                                    logging.info(
                                        "Downloading %s/%s: %.1f%% (%d/%d bytes)",
                                        repo_id,
                                        filename,
                                        percent,
                                        downloaded_bytes,
                                        total_bytes,
                                    )
                            else:
                                logging.info(
                                    "Downloading %s/%s: %d bytes",
                                    repo_id,
                                    filename,
                                    downloaded_bytes,
                                )

            tmp.replace(target)

            logging.info(
                "Download ready %s/%s: %s",
                repo_id,
                filename,
                target,
            )

            return target

        except Exception:
            logging.exception("Failed downloading %s/%s", repo_id, filename)

            if tmp.exists():
                tmp.unlink()

            raise
