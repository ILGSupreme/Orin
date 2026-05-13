from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppError(Exception):
    message: str
    code: str = "app_error"
    status_code: int = 500
    client_details: dict[str, Any] = field(default_factory=dict)
    internal_details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass
class UpstreamError(AppError):
    provider: str = "unknown"
    upstream_status: int | None = None

    def __post_init__(self):
        if self.code == "app_error":
            self.code = "upstream_error"
        if self.status_code == 500:
            self.status_code = 502


@dataclass
class ValidationError(AppError):
    def __post_init__(self):
        if self.code == "app_error":
            self.code = "validation_error"
        if self.status_code == 500:
            self.status_code = 400


@dataclass
class NotFoundError(AppError):
    resource: str = "resource"

    def __post_init__(self):
        if self.code == "app_error":
            self.code = "not_found"
        if self.status_code == 500:
            self.status_code = 404


@dataclass
class RateLimitError(AppError):
    retry_after: int | None = None

    def __post_init__(self):
        if self.code == "app_error":
            self.code = "rate_limited"
        if self.status_code == 500:
            self.status_code = 429
