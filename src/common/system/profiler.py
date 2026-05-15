from __future__ import annotations

import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from torch.cuda import mem_get_info

from common.system.model_inspector import inspect_model
from common.types import ModelProfile, SystemConstraints

logging.getLogger(__name__)


def _read_first_existing(paths: list[str]) -> str | None:
    for p in paths:
        path = Path(p)
        try:
            if path.exists():
                return path.read_text(errors="ignore").strip("\x00").strip()
        except Exception:
            continue
    return None


def detect_unified_memory() -> tuple[bool | None, str]:
    """
    Best-effort detection of unified/shared memory.

    Returns:
        (is_unified, reason)

        is_unified:
            True  -> likely unified/shared memory
            False -> likely discrete/dedicated memory
            None  -> undetermined
    """
    arch = platform.machine().lower()

    model = _read_first_existing(
        [
            "/proc/device-tree/model",
            "/sys/firmware/devicetree/base/model",
        ]
    )
    compatible = _read_first_existing(
        [
            "/proc/device-tree/compatible",
            "/sys/firmware/devicetree/base/compatible",
        ]
    )

    model_l = (model or "").lower()
    compatible_l = (compatible or "").lower()

    # Jetson / Tegra is the strongest signal here
    if "jetson" in model_l or "tegra" in model_l or "nvidia,tegra" in compatible_l:
        return True, "Jetson/Tegra platform detected"

    if (
        "raspberry pi" in model_l
        or "raspberrypi" in model_l
        or "brcm,bcm" in compatible_l
    ):
        return True, "Raspberry Pi / Broadcom SoC detected"

    if "apple," in compatible_l:
        return True, "Apple Silicon SoC detected"

    # Integrated graphics hint via lspci, when available
    try:
        output = subprocess.check_output(
            "lspci | grep -Ei 'vga|display|3d'",
            shell=True,
            stderr=subprocess.DEVNULL,
            text=True,
        ).lower()

        if "intel" in output:
            return True, "Intel integrated graphics detected"
        if "amd" in output and "apu" in output:
            return True, "AMD integrated graphics detected"
        if "nvidia" in output:
            return False, "NVIDIA PCIe GPU detected"
    except Exception:
        pass

    # --- AMD Nuance Check ---
    try:
        # Check lspci for any AMD graphics devices
        lspci_out = subprocess.check_output("lspci", shell=True, text=True).lower()
        if "amd" in lspci_out or "advanced micro devices" in lspci_out:
            # Check sysfs for integrated GPU signal: in1_input existence
            # This file typically exists only for AMD integrated GPUs (APUs)
            is_integrated = False
            for card_path in Path("/sys/class/drm/").glob("card*"):
                hwmon_path = card_path / "device/hwmon"
                if hwmon_path.exists():
                    for hwmon_dir in hwmon_path.iterdir():
                        if (hwmon_dir / "in1_input").exists():
                            is_integrated = True
                            break

            if is_integrated:
                return True, "AMD Integrated Graphics (APU) detected"

            # If AMD is in lspci but no integrated signal found,
            # and no other dGPU is present, it might still be a dGPU.
            # Usually, if it's a dedicated card, it won't have that specific hwmon file.
            return (
                False,
                "AMD GPU detected, but integrated/discrete status is unconfirmed",
            )
    except Exception:
        pass

    # ARM alone is not proof, but can be a weak hint
    if arch in {"aarch64", "arm64", "armv7l"}:
        return None, "ARM architecture detected, but unified memory not confirmed"

    return None, "Could not determine memory architecture"


def get_linux_info() -> dict[str, str]:
    info = {
        "OS": platform.system(),
        "Distro": "Unknown",
        "Kernel": platform.release(),
        "Architecture": platform.machine(),
        "Device Model": "Unknown",
        "Unified Memory": "Undetermined",
        "Unified Memory Reason": "No signal detected",
    }

    if os.path.exists("/etc/os-release"):
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        info["Distro"] = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass

    model = _read_first_existing(
        [
            "/proc/device-tree/model",
            "/sys/firmware/devicetree/base/model",
        ]
    )
    if model:
        info["Device Model"] = model

    unified, reason = detect_unified_memory()
    if unified is True:
        info["Unified Memory"] = "Yes"
    elif unified is False:
        info["Unified Memory"] = "No"
    else:
        info["Unified Memory"] = "Undetermined"

    info["Unified Memory Reason"] = reason
    return info


def estimate_kv_bytes_per_token(
    *,
    block_count: int,
    head_count_kv: int,
    key_length: int,
    value_length: int,
    bytes_per_elem: int = 2,
    active_kv_fraction: float = 1.0,
) -> int:
    """
    Estimate KV-cache bytes per token from GGUF-style metadata.

    Parameters
    ----------
    block_count:
        Total transformer layer count from metadata.
    head_count_kv:
        Number of KV heads.
    key_length:
        Per-head key width.
    value_length:
        Per-head value width.
    bytes_per_elem:
        Bytes per KV element. FP16 KV cache is typically 2.
    active_kv_fraction:
        Fraction of layers that actually store KV.
        Use 1.0 for normal full-attention models.
        Use e.g. 0.25 for hybrid architectures if only 25% of layers use KV.

    Returns
    -------
    int
        Estimated KV-cache bytes per token.
    """
    if block_count <= 0:
        raise ValueError("block_count must be > 0")
    if head_count_kv <= 0:
        raise ValueError("head_count_kv must be > 0")
    if key_length <= 0:
        raise ValueError("key_length must be > 0")
    if value_length <= 0:
        raise ValueError("value_length must be > 0")
    if bytes_per_elem <= 0:
        raise ValueError("bytes_per_elem must be > 0")
    if not (0 < active_kv_fraction <= 1.0):
        raise ValueError("active_kv_fraction must be in (0, 1]")

    active_layers = max(1, round(block_count * active_kv_fraction))

    return active_layers * head_count_kv * (key_length + value_length) * bytes_per_elem


def estimate_max_n_ctx(
    *,
    total_vram_bytes: int,
    model_residency_bytes: int,
    kv_bytes_per_token: int,
    reserve_bytes: int,
    safety_margin_bytes: int,
    alignment: int = 256,
) -> int:
    """
    Estimate the maximum safe n_ctx that fits in VRAM.

    Parameters
    ----------
    total_vram_bytes:
        Total usable VRAM on the target device.
    model_residency_bytes:
        Estimated VRAM consumed by model weights for the chosen offload profile.
    kv_bytes_per_token:
        Output from estimate_kv_bytes_per_token().
    reserve_bytes:
        Extra VRAM reserved for compute buffers / runtime overhead.
    safety_margin_bytes:
        Extra headroom to avoid running at the limit.
    alignment:
        Round the result down to a practical multiple, default 256.

    Returns
    -------
    int
        Estimated safe n_ctx rounded down to alignment.
    """
    if total_vram_bytes <= 0:
        raise ValueError("total_vram_bytes must be > 0")
    if model_residency_bytes < 0:
        raise ValueError("model_residency_bytes must be >= 0")
    if kv_bytes_per_token <= 0:
        raise ValueError("kv_bytes_per_token must be > 0")
    if reserve_bytes < 0:
        raise ValueError("reserve_bytes must be >= 0")
    if safety_margin_bytes < 0:
        raise ValueError("safety_margin_bytes must be >= 0")
    if alignment <= 0:
        raise ValueError("alignment must be > 0")

    available_for_kv = (
        total_vram_bytes - model_residency_bytes - reserve_bytes - safety_margin_bytes
    )

    if available_for_kv <= 0:
        return 0

    raw_n_ctx = available_for_kv // kv_bytes_per_token
    aligned_n_ctx = (raw_n_ctx // alignment) * alignment

    return max(0, aligned_n_ctx)


def estimate_model_residency(
    *,
    model_size_bytes: int,
    total_layers: int,
    n_gpu_layers: int,
) -> int:
    """
    Estimate how many model-weight bytes will reside in VRAM.

    This is a simple proportional estimator:
    - if n_gpu_layers <= 0, nothing is placed on GPU
    - if n_gpu_layers >= total_layers, the full model is assumed on GPU
    - otherwise, VRAM residency is estimated as a linear fraction of model size

    Parameters
    ----------
    model_size_bytes:
        Total model file size in bytes.
    total_layers:
        Total transformer layer count.
    n_gpu_layers:
        Number of layers to offload to GPU.

    Returns
    -------
    int
        Estimated model residency in VRAM, in bytes.
    """
    if model_size_bytes <= 0:
        raise ValueError("model_size_bytes must be > 0")
    if total_layers <= 0:
        raise ValueError("total_layers must be > 0")

    gpu_layers_clamped = max(0, min(n_gpu_layers, total_layers))

    if gpu_layers_clamped == 0:
        return 0
    if gpu_layers_clamped == total_layers:
        return model_size_bytes

    offload_fraction = gpu_layers_clamped / total_layers
    return int(model_size_bytes * offload_fraction)


def clamp_n_ctx(
    *,
    estimated_n_ctx: int,
    model_max_context: int | None = None,
    minimum_n_ctx: int = 1024,
    alignment: int = 256,
) -> int:
    """
    Clamp and align n_ctx to a valid runtime value.

    Rules:
    - rounds down to the chosen alignment
    - enforces a minimum
    - optionally clamps to the model's advertised max context

    Parameters
    ----------
    estimated_n_ctx:
        Raw estimated context size.
    model_max_context:
        Optional maximum context supported by the model.
    minimum_n_ctx:
        Minimum acceptable context size.
    alignment:
        Round result down to this multiple.

    Returns
    -------
    int
        Final clamped/aligned n_ctx.
    """
    if estimated_n_ctx < 0:
        raise ValueError("estimated_n_ctx must be >= 0")
    if model_max_context is not None and model_max_context <= 0:
        raise ValueError("model_max_context must be > 0 when provided")
    if minimum_n_ctx <= 0:
        raise ValueError("minimum_n_ctx must be > 0")
    if alignment <= 0:
        raise ValueError("alignment must be > 0")

    clamped = estimated_n_ctx

    if model_max_context is not None:
        clamped = min(clamped, model_max_context)

    clamped = max(clamped, minimum_n_ctx)
    clamped = (clamped // alignment) * alignment

    return max(alignment, clamped)


def build_profile(
    *,
    total_vram_bytes: int,
    model_size_bytes: int,
    total_layers: int,
    n_gpu_layers: list[int],
    block_count: int,
    head_count_kv: int,
    key_length: int,
    value_length: int,
    model_max_context: int | None = None,
    bytes_per_elem: int = 2,
    active_kv_fraction: float = 1.0,
    reserve_bytes: int = 0,
    safety_margin_bytes: int = 0,
    minimum_n_ctx: int = 1024,
    alignment: int = 256,
    profile_factors: dict[str, float] | None,
) -> dict[str, ModelProfile]:
    """
    Build a VRAM-based runtime profile for a model load.

    Returns upper-bound estimates plus recommended conservative/balanced/aggressive
    n_ctx values derived from that upper bound.
    """
    profiles: dict[str, Any] = {}
    profile_nbatch: dict[str, Any] = {
        "conservative": 256,
        "balanced": 512,
        "aggressive": 1024,
    }
    if profile_factors is None:
        profile_factors = {
            "conservative": 0.50,
            "balanced": 0.70,
            "aggressive": 0.85,
        }
    for index, (profile_name, factor) in enumerate(profile_factors.items()):
        model_residency_bytes = estimate_model_residency(
            model_size_bytes=model_size_bytes,
            total_layers=total_layers,
            n_gpu_layers=n_gpu_layers[index],
        )

        kv_bytes_per_token = estimate_kv_bytes_per_token(
            block_count=block_count,
            head_count_kv=head_count_kv,
            key_length=key_length,
            value_length=value_length,
            bytes_per_elem=bytes_per_elem,
            active_kv_fraction=active_kv_fraction,
        )

        estimated_upper_n_ctx = estimate_max_n_ctx(
            total_vram_bytes=total_vram_bytes,
            model_residency_bytes=model_residency_bytes,
            kv_bytes_per_token=kv_bytes_per_token,
            reserve_bytes=reserve_bytes,
            safety_margin_bytes=safety_margin_bytes,
            alignment=alignment,
        )

        estimated_upper_n_ctx = clamp_n_ctx(
            estimated_n_ctx=estimated_upper_n_ctx,
            model_max_context=model_max_context,
            minimum_n_ctx=minimum_n_ctx,
            alignment=alignment,
        )

        recommended_n_ctx = 0

        raw_value = int(estimated_upper_n_ctx * factor)
        recommended_n_ctx = clamp_n_ctx(
            estimated_n_ctx=raw_value,
            model_max_context=model_max_context,
            minimum_n_ctx=minimum_n_ctx,
            alignment=alignment,
        )

        profiles[profile_name] = ModelProfile.model_validate(
            {
                "designation": profile_name,
                "inputs": {
                    "total_vram_bytes": total_vram_bytes,
                    "model_size_bytes": model_size_bytes,
                    "total_layers": total_layers,
                    "n_gpu_layers": n_gpu_layers[index],
                    "block_count": block_count,
                    "head_count_kv": head_count_kv,
                    "key_length": key_length,
                    "value_length": value_length,
                    "model_max_context": model_max_context,
                    "bytes_per_elem": bytes_per_elem,
                    "active_kv_fraction": active_kv_fraction,
                    "reserve_bytes": reserve_bytes,
                    "safety_margin_bytes": safety_margin_bytes,
                    "minimum_n_ctx": minimum_n_ctx,
                    "alignment": alignment,
                },
                "estimates": {
                    "model_residency_bytes": model_residency_bytes,
                    "kv_bytes_per_token": kv_bytes_per_token,
                    "estimated_upper_n_ctx": estimated_upper_n_ctx,
                },
                "recommended_n_ctx": recommended_n_ctx,
                "n_batch": profile_nbatch[profile_name],
            }
        )
    return profiles


def kv_bytes_per_elem_from_cache_type(cache_type: str) -> float:
    mapping = {
        "f32": 4.0,
        "f16": 2.0,
        "bf16": 2.0,
        "q8_0": 1.0,  # approximate effective bytes/elem
        "q4_0": 0.5,  # approximate effective bytes/elem
        "q4_1": 0.5,
        "iq4_nl": 0.5,
        "q5_0": 0.625,
        "q5_1": 0.625,
    }
    key = cache_type.strip().lower()
    if key not in mapping:
        raise ValueError(f"Unsupported cache_type: {cache_type}")

    return mapping[cache_type]


def mib(x: int) -> int:
    return x * 1024 * 1024


def gib(x: int) -> int:
    return x * 1024 * 1024 * 1024


def inspect_system() -> SystemConstraints:
    return SystemConstraints(
        cpu_count=os.cpu_count() or 1,
        ram_total_bytes=read_meminfo_value("MemTotal"),
        ram_available_bytes=read_meminfo_value("MemAvailable"),
        gpu_total_bytes=read_gpu_mem_total(),
        gpu_free_bytes=read_gpu_mem_free(),
    )


def read_meminfo_value(key: str) -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(f"{key}:"):
                parts = line.split()
                kb = int(parts[1])
                return kb * 1024
    except Exception:
        return 0
    return 0


def get_ngpu_layers_by_profile_factor(
    total_layers: int, profile_factors: dict[str, float]
) -> list[int]:
    values = []
    for factor in profile_factors.values():
        if not (0 < factor <= 1.0):
            raise ValueError(f"Invalid profile factor: {factor}")
        n_gpu = max(0, min(total_layers, int(total_layers * factor)))
        values.append(n_gpu)
    return values


def read_gpu_mem_total() -> int:
    _, total = mem_get_info()
    return total


def read_gpu_mem_free() -> int:
    free, _ = mem_get_info()
    return free


def get_model_profile(
    path: str,
    reserve_size: int,
    safety_size: int,
    profile_factors: dict[str, float],
    cache_type="f16",
):
    logging.info(
        f"Entering get model profile, reserve: path: {path}, {reserve_size}, safety: {safety_size}, profile_factors: {profile_factors}"
    )
    model_metadata = inspect_model(path)
    #system_info = get_linux_info()
    memory_info = inspect_system()

    n_gpus = get_ngpu_layers_by_profile_factor(
        total_layers=model_metadata.block_count, profile_factors=profile_factors
    )
    bytes_per_element = kv_bytes_per_elem_from_cache_type(cache_type=cache_type)

    return build_profile(
        total_vram_bytes=memory_info.gpu_free_bytes,
        model_size_bytes=model_metadata.file_size_bytes,
        block_count=model_metadata.block_count,
        total_layers=model_metadata.block_count,
        n_gpu_layers=n_gpus,
        head_count_kv=model_metadata.head_count_kv,
        key_length=model_metadata.key_length,
        value_length=model_metadata.value_length,
        model_max_context=model_metadata.context_length,
        reserve_bytes=reserve_size,
        safety_margin_bytes=safety_size,
        bytes_per_elem=bytes_per_element,
        profile_factors=profile_factors,
    )
