from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field

MEMORY_OPERATIONS = Literal[
    'create_summary',
    'list_summaries',
    'create_note',
    'list_notes',
    'list_memory_events',
    'create_memory_claim',
    'list_memory_claims',
    'search_memory_claims',
    'retrieve',
    'prompt_context',
    'upsert_user',
    'create_session',
    'resolve_session',
    'create_message',
    'list_recent_messages']

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

ModelFormat = Literal["gguf", "unknown"]

class SystemConstraints(BaseModel):
    cpu_count: int
    ram_total_bytes: int
    ram_available_bytes: int
    gpu_total_bytes: int
    gpu_free_bytes: int


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
    total_vram_bytes: int = 0
    model_size_bytes: int = 0
    total_layers: int = 0
    n_gpu_layers: int = 0
    block_count: int = 0
    head_count_kv: int = 0
    key_length: int = 0
    value_length: int = 0
    model_max_context: int = 0
    bytes_per_elem: int = 0
    active_kv_fraction: int = 0
    reserve_bytes: int = 0
    safety_margin_bytes: int = 0
    minimum_n_ctx: int = 0
    alignment: int = 0


class ModelEstimates(BaseModel):
    model_residency_bytes: int = 0
    kv_bytes_per_token: int = 0
    estimated_upper_n_ctx: int = 0

class MachineInfo(BaseModel):
    operating_system: str | None = None
    distro: str | None = None
    kernel: str | None = None
    architecture: str | None = None
    device_model: str | None = None
    unified_memory: str | None = None
    unified_memory_reason: str | None = None

class ModelProfile(BaseModel):
    designation: str = "conservative"
    inputs: ModelInputs = Field(default_factory=ModelInputs)
    estimates: ModelEstimates = Field(default_factory=ModelEstimates)
    recommended_n_ctx: int = 0
    n_batch: int = 0

class Profile(BaseModel):
    machine_info: MachineInfo = Field(default_factory=MachineInfo)
    profiles: dict[str, ModelProfile] = Field(default_factory=dict)

    reserve_size:int = 1024 * 1024 * 1024
    safety_size:int = 1024 * 1024 * 1024
    runtime_profiles:dict[str,float] = {"conservative": 0.5, "balanced": 0.7, "aggressive": 0.85}

    current_profile: Literal['conservative', "balanced", "aggressive"] = "conservative"

    def set_machine_info(self, info:dict[str,Any]):
        try:
            self.machine_info = MachineInfo.model_validate(info)
        except Exception as e:
            raise e
        
    def set_profiles(self, profiles: dict[str, ModelProfile]):
        self.profiles = profiles
    
    def get_current_profile(self):
        profile = self.profiles.get(self.current_profile)
        if profile:
            return profile
        raise ValueError(f"Profile: {self.current_profile} not found")
