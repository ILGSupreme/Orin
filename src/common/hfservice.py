from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download
from huggingface_hub.errors import HfHubHTTPError


@dataclass(slots=True)
class HFSearchResult:
    repo_id: str
    author: str | None
    sha: str | None
    private: bool | None
    gated: str | bool | None
    downloads: int | None
    likes: int | None
    tags: list[str]
    pipeline_tag: str | None
    library_name: str | None
    created_at: str | None
    last_modified: str | None


@dataclass(slots=True)
class HFRepoSummary:
    repo_id: str
    sha: str | None
    private: bool | None
    gated: str | bool | None
    downloads: int | None
    likes: int | None
    tags: list[str]
    pipeline_tag: str | None
    library_name: str | None
    siblings: list[str]


@dataclass(slots=True)
class HFDownloadedFile:
    repo_id: str
    filename: str
    revision: str | None
    local_path: str
    size_bytes: int | None


class HuggingFaceService:
    def __init__(
        self,
        *,
        token: str | bool | None = None,
        endpoint: str | None = None,
        cache_dir: str | None = None,
        library_name: str = "orin-cluster",
        library_version: str = "0.1.0",
    ) -> None:
        self.api = HfApi(
            endpoint=endpoint,
            token=token,
            library_name=library_name,
            library_version=library_version,
        )
        self.cache_dir = cache_dir

    def search_models(
        self,
        *,
        query: str,
        limit: int = 20,
        tags: list[str] | None = None,
        author: str | None = None,
        sort: str | None = "downloads",
        direction: int | None = -1,
        gated: bool | None = None,
    ) -> list[HFSearchResult]:
        """
        Search model repos on Hugging Face Hub.
        """
        try:
            results = self.api.list_models(
                search=query,
                limit=limit,
                tags=tags,
                author=author,
                sort=sort,
                direction=direction,
                gated=gated,
                full=True,
            )
            items: list[HFSearchResult] = []
            for m in results:
                items.append(
                    HFSearchResult(
                        repo_id=m.id,
                        author=getattr(m, "author", None),
                        sha=getattr(m, "sha", None),
                        private=getattr(m, "private", None),
                        gated=getattr(m, "gated", None),
                        downloads=getattr(m, "downloads", None),
                        likes=getattr(m, "likes", None),
                        tags=list(getattr(m, "tags", []) or []),
                        pipeline_tag=getattr(m, "pipeline_tag", None),
                        library_name=getattr(m, "library_name", None),
                        created_at=str(getattr(m, "created_at", None))
                        if getattr(m, "created_at", None)
                        else None,
                        last_modified=str(getattr(m, "last_modified", None))
                        if getattr(m, "last_modified", None)
                        else None,
                    )
                )
            return items
        except HfHubHTTPError as exc:
            raise RuntimeError(f"Hugging Face search failed: {exc}") from exc

    def get_model_info(
        self, repo_id: str, *, revision: str | None = None
    ) -> HFRepoSummary:
        """
        Fetch detailed repo metadata, including file list ('siblings').
        """
        try:
            info = self.api.model_info(
                repo_id=repo_id, revision=revision, files_metadata=False
            )
            siblings = [s.rfilename for s in (getattr(info, "siblings", None) or [])]
            return HFRepoSummary(
                repo_id=info.id,
                sha=getattr(info, "sha", None),
                private=getattr(info, "private", None),
                gated=getattr(info, "gated", None),
                downloads=getattr(info, "downloads", None),
                likes=getattr(info, "likes", None),
                tags=list(getattr(info, "tags", []) or []),
                pipeline_tag=getattr(info, "pipeline_tag", None),
                library_name=getattr(info, "library_name", None),
                siblings=siblings,
            )
        except HfHubHTTPError as exc:
            raise RuntimeError(
                f"Failed to get model info for {repo_id}: {exc}"
            ) from exc

    def list_repo_files(
        self, repo_id: str, *, revision: str | None = None
    ) -> list[str]:
        """
        List file paths in a model repo.
        """
        try:
            return list(
                self.api.list_repo_files(
                    repo_id=repo_id, repo_type="model", revision=revision
                )
            )
        except HfHubHTTPError as exc:
            raise RuntimeError(f"Failed to list files for {repo_id}: {exc}") from exc

    def find_gguf_files(
        self, repo_id: str, *, revision: str | None = None
    ) -> list[str]:
        """
        Convenience helper for GGUF repos.
        """
        files = self.list_repo_files(repo_id, revision=revision)
        return sorted([f for f in files if f.lower().endswith(".gguf")])

    def download_file(
        self,
        *,
        repo_id: str,
        filename: str,
        revision: str | None = None,
        local_dir: str | None = None,
        local_dir_use_symlinks: bool | str = "auto",
    ) -> HFDownloadedFile:
        """
        Download one file into the HF cache or an explicit local dir.
        """
        try:
            local_path = hf_hub_download(
                repo_id=repo_id,
                filename=filename,
                repo_type="model",
                revision=revision,
                cache_dir=self.cache_dir,
                local_dir=local_dir,
                local_dir_use_symlinks=local_dir_use_symlinks,
            )
            p = Path(local_path)
            return HFDownloadedFile(
                repo_id=repo_id,
                filename=filename,
                revision=revision,
                local_path=str(p),
                size_bytes=p.stat().st_size if p.exists() else None,
            )
        except HfHubHTTPError as exc:
            raise RuntimeError(
                f"Failed to download {repo_id}/{filename}: {exc}"
            ) from exc

    def choose_gguf_file(
        self,
        repo_id: str,
        *,
        preferred_quant_keywords: list[str] | None = None,
        revision: str | None = None,
    ) -> str | None:
        """
        Pick a GGUF file from a repo. Simple heuristic:
        - prefer files containing any preferred quant keyword
        - otherwise return the shortest GGUF filename
        """
        ggufs = self.find_gguf_files(repo_id, revision=revision)
        if not ggufs:
            return None

        if preferred_quant_keywords:
            lowered = [(f, f.lower()) for f in ggufs]
            for keyword in preferred_quant_keywords:
                keyword_l = keyword.lower()
                for original, lowered_name in lowered:
                    if keyword_l in lowered_name:
                        return original

        return sorted(ggufs, key=len)[0]

    def repo_has_file(
        self,
        *,
        repo_id: str,
        filename: str,
        revision: str | None = None,
    ) -> bool:
        files = self.list_repo_files(repo_id, revision=revision)
        return filename in files
