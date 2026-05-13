from typing import Any, Literal

from pydantic import BaseModel, Field

RUNTIMEPROFILES = {"conservative": 0.5, "balanced": 0.7, "aggressive": 0.85}
RESERVE_SIZE = 1024 * 1024 * 1024
SAFETY_SIZE = 1024 * 1024 * 1024

SAFETY_TOKEN_SIZE = 64

MAX_TOKENS_POLICY = {
    "chat": 384,
    "summarize": 640,
    "classify": 64,
    "extract": 128,
    "analyze": 900,
    "search": 256,
    "inspect": 256,
}

TEMPERATURE_POLICY = {
    "chat": 0.7,
    "summarize": 0.3,
    "classify": 0.1,
    "extract": 0.1,
    "analyze": 0.4,
    "search": 0.2,
    "inspect": 0.1,
}


class SystemConstraints(BaseModel):
    cpu_count: int
    ram_total_bytes: int
    ram_available_bytes: int
    gpu_total_bytes: int
    gpu_free_bytes: int


ModelFormat = Literal["gguf", "unknown"]


class ModelMetadata(BaseModel):
    path: str
    format: ModelFormat
    file_size_bytes: int

    architecture: str | None = None
    name: str | None = None
    basename: str | None = None
    size_label: str | None = None
    license: str | None = None

    gguf_version: int | None = None
    tensor_count: int | None = None
    kv_count: int | None = None

    file_type: int | None = None
    quantization_version: int | None = None

    block_count: int | None = None
    context_length: int | None = None
    embedding_length: int | None = None
    head_count: int | None = None
    head_count_kv: int | None = None
    key_length: int | None = None
    value_length: int | None = None
    rope_freq_base: float | None = None

    tokenizer_model: str | None = None
    tokenizer_pre: str | None = None
    eos_token_id: int | None = None
    padding_token_id: int | None = None

    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class ModelInputs(BaseModel):
    total_vram_bytes: int
    model_size_bytes: int
    total_layers: int
    n_gpu_layers: int
    block_count: int
    head_count_kv: int
    key_length: int
    value_length: int
    model_max_context: int
    bytes_per_elem: int
    active_kv_fraction: int
    reserve_bytes: int
    safety_margin_bytes: int
    minimum_n_ctx: int
    alignment: int


class ModelEstimates(BaseModel):
    model_residency_bytes: int
    kv_bytes_per_token: int
    estimated_upper_n_ctx: int


class ModelProfile(BaseModel):
    inputs: ModelInputs
    estimates: ModelEstimates
    recommended_n_ctx: int
    n_batch: int


class LoadSettings(BaseModel):
    n_gpu_layers: int
    n_batch: int
    n_ctx: int
    n_threads: int
