from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

from common.types import ModelMetadata

# GGUF value type tags
GGUF_TYPE_UINT8 = 0
GGUF_TYPE_INT8 = 1
GGUF_TYPE_UINT16 = 2
GGUF_TYPE_INT16 = 3
GGUF_TYPE_UINT32 = 4
GGUF_TYPE_INT32 = 5
GGUF_TYPE_FLOAT32 = 6
GGUF_TYPE_BOOL = 7
GGUF_TYPE_STRING = 8
GGUF_TYPE_ARRAY = 9
GGUF_TYPE_UINT64 = 10
GGUF_TYPE_INT64 = 11
GGUF_TYPE_FLOAT64 = 12


def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise EOFError(f"Expected {n} bytes, got {len(data)}")
    return data


def _u32(f) -> int:
    return struct.unpack("<I", _read_exact(f, 4))[0]


def _u64(f) -> int:
    return struct.unpack("<Q", _read_exact(f, 8))[0]


def _i8(f) -> int:
    return struct.unpack("<b", _read_exact(f, 1))[0]


def _u8(f) -> int:
    return struct.unpack("<B", _read_exact(f, 1))[0]


def _i16(f) -> int:
    return struct.unpack("<h", _read_exact(f, 2))[0]


def _u16(f) -> int:
    return struct.unpack("<H", _read_exact(f, 2))[0]


def _i32(f) -> int:
    return struct.unpack("<i", _read_exact(f, 4))[0]


def _u32_val(f) -> int:
    return struct.unpack("<I", _read_exact(f, 4))[0]


def _i64(f) -> int:
    return struct.unpack("<q", _read_exact(f, 8))[0]


def _u64_val(f) -> int:
    return struct.unpack("<Q", _read_exact(f, 8))[0]


def _f32(f) -> float:
    return struct.unpack("<f", _read_exact(f, 4))[0]


def _f64(f) -> float:
    return struct.unpack("<d", _read_exact(f, 8))[0]


def _bool(f) -> bool:
    return struct.unpack("<?", _read_exact(f, 1))[0]


def _gguf_string(f) -> str:
    # GGUF strings are length-prefixed with u64
    n = _u64(f)
    raw = _read_exact(f, n)
    return raw.decode("utf-8")


def _read_value(f, value_type: int) -> Any:
    if value_type == GGUF_TYPE_UINT8:
        return _u8(f)
    if value_type == GGUF_TYPE_INT8:
        return _i8(f)
    if value_type == GGUF_TYPE_UINT16:
        return _u16(f)
    if value_type == GGUF_TYPE_INT16:
        return _i16(f)
    if value_type == GGUF_TYPE_UINT32:
        return _u32_val(f)
    if value_type == GGUF_TYPE_INT32:
        return _i32(f)
    if value_type == GGUF_TYPE_FLOAT32:
        return _f32(f)
    if value_type == GGUF_TYPE_BOOL:
        return _bool(f)
    if value_type == GGUF_TYPE_STRING:
        return _gguf_string(f)
    if value_type == GGUF_TYPE_UINT64:
        return _u64_val(f)
    if value_type == GGUF_TYPE_INT64:
        return _i64(f)
    if value_type == GGUF_TYPE_FLOAT64:
        return _f64(f)
    if value_type == GGUF_TYPE_ARRAY:
        elem_type = _u32(f)
        length = _u64(f)
        return [_read_value(f, elem_type) for _ in range(length)]

    raise ValueError(f"Unsupported GGUF value type: {value_type}")


def read_gguf_metadata(model_path: str | Path) -> dict[str, Any]:
    """
    Reads only the GGUF metadata KV section, not the tensor data.
    Supports GGUF v2/v3 style metadata parsing for common scalar/string/array types.
    """
    path = Path(model_path)
    result: dict[str, Any] = {
        "_path": str(path),
        "_file_size_bytes": path.stat().st_size,
    }

    with path.open("rb") as f:
        magic = _read_exact(f, 4)
        if magic != b"GGUF":
            raise ValueError(f"{path} is not a GGUF file")

        version = _u32(f)
        if version not in (2, 3):
            raise ValueError(f"Unsupported GGUF version: {version}")

        tensor_count = _u64(f)
        kv_count = _u64(f)

        result["_gguf_version"] = version
        result["_tensor_count"] = tensor_count
        result["_kv_count"] = kv_count

        for _ in range(kv_count):
            key = _gguf_string(f)
            value_type = _u32(f)
            value = _read_value(f, value_type)
            result[key] = value

    return result


def summarize_gguf_metadata(model_path: str | Path) -> dict[str, Any]:
    """
    Extracts the metadata you are most likely to care about for load-profile estimation.
    """
    meta = read_gguf_metadata(model_path)
    arch = meta.get("general.architecture")

    def pick(*keys: str) -> Any:
        for key in keys:
            if key in meta:
                return meta[key]
        return None

    summary = {
        "model_path": str(model_path),
        "file_size_bytes": meta.get("_file_size_bytes"),
        "gguf_version": meta.get("_gguf_version"),
        "tensor_count": meta.get("_tensor_count"),
        "kv_count": meta.get("_kv_count"),
        "architecture": arch,
        "name": meta.get("general.name"),
        "basename": meta.get("general.basename"),
        "size_label": meta.get("general.size_label"),
        "license": meta.get("general.license"),
        "file_type": meta.get("general.file_type"),
        "quantization_version": meta.get("general.quantization_version"),
        "block_count": pick(f"{arch}.block_count") if arch else None,
        "context_length": pick(f"{arch}.context_length") if arch else None,
        "embedding_length": pick(f"{arch}.embedding_length") if arch else None,
        "head_count": pick(f"{arch}.attention.head_count") if arch else None,
        "head_count_kv": pick(f"{arch}.attention.head_count_kv") if arch else None,
        "key_length": pick(f"{arch}.attention.key_length") if arch else None,
        "value_length": pick(f"{arch}.attention.value_length") if arch else None,
        "rope_freq_base": pick(f"{arch}.rope.freq_base") if arch else None,
        "tokenizer_model": meta.get("tokenizer.ggml.model"),
        "tokenizer_pre": meta.get("tokenizer.ggml.pre"),
        "eos_token_id": meta.get("tokenizer.ggml.eos_token_id"),
        "padding_token_id": meta.get("tokenizer.ggml.padding_token_id"),
    }
    return summary


def inspect_model(model_path: str | Path) -> ModelMetadata:
    path = Path(model_path)

    if path.suffix.lower() == ".gguf":
        summary = summarize_gguf_metadata(path)
        raw = read_gguf_metadata(path)

        return ModelMetadata(
            path=str(path),
            format="gguf",
            file_size_bytes=summary["file_size_bytes"],
            architecture=summary.get("architecture"),
            name=summary.get("name"),
            basename=summary.get("basename"),
            size_label=summary.get("size_label"),
            license=summary.get("license"),
            gguf_version=summary.get("gguf_version"),
            tensor_count=summary.get("tensor_count"),
            kv_count=summary.get("kv_count"),
            file_type=summary.get("file_type"),
            quantization_version=summary.get("quantization_version"),
            block_count=summary.get("block_count"),
            context_length=summary.get("context_length"),
            embedding_length=summary.get("embedding_length"),
            head_count=summary.get("head_count"),
            head_count_kv=summary.get("head_count_kv"),
            key_length=summary.get("key_length"),
            value_length=summary.get("value_length"),
            rope_freq_base=summary.get("rope_freq_base"),
            tokenizer_model=summary.get("tokenizer_model"),
            tokenizer_pre=summary.get("tokenizer_pre"),
            eos_token_id=summary.get("eos_token_id"),
            padding_token_id=summary.get("padding_token_id"),
            raw_metadata=raw,
        )

    return ModelMetadata(
        path=str(path),
        format="unknown",
        file_size_bytes=path.stat().st_size,
    )
