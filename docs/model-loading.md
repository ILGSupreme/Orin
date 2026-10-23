# GGUF model loading and memory configuration

GGUF loads resolve/download the model first, then wait for current generation,
close the old native model, and measure free GPU memory before profiling the new
one. A load that fails after unloading leaves inference unavailable; load a
known-good model again to recover. Repeated loads reprofile and reload even when
the model ID is unchanged. Caller cancellation does not release the generation
lock until the underlying load completes.

## Configuration

Set defaults under `primer.gguf_load` in the service configuration, or supply
`gguf_load` in a POST /load request. A supplied request object replaces the
configuration object in full; omitted members use the defaults below.

| Field | Default | Meaning |
|---|---|---|
| n_gpu_layers | null | Automatic profile selection; otherwise 0 through the model layer count |
| n_ctx | null | Automatic context selection; otherwise at least 1024, in multiples of 256 |
| reserve_bytes | 1073741824 | Memory reserve excluded from the estimate |
| safety_margin_bytes | 1073741824 | Additional memory headroom excluded from the estimate |

All numeric overrides must be integers; negative values and unknown option keys
are rejected. Explicit context is independent of the profile factor, but cannot
exceed the estimated memory capacity or model context limit. Automatic profiles
also reject an estimated capacity below the minimum instead of rounding upward
to a configuration that does not fit. These are estimates, not OOM guarantees:
runtime buffers, device activity and architecture-specific allocations can differ.
The estimator still budgets the full KV cache against GPU memory, including when
few or no layers are offloaded. CPU-only sizing and quantized KV caches are not
implemented here.

The historical `n_gpu_layer` and `max_model_len` fields are not overrides for
GGUF profiling; use this explicit object. Non-GGUF engines reject request-level
GGUF overrides. Existing configuration without this object keeps the prior
automatic layer/context choices and 1 GiB margins when the estimate fits.

Example candidate for the observed 4B model and approximately 3.50 GiB free GPU
memory (requires live validation; this is not a recommended universal preset):

```json
{
  "provider": "huggingface",
  "engine": "gguf",
  "model_id": "qwen35-4b",
  "repo_id": "unsloth/Qwen3.5-4b-GGUF",
  "filename": "Qwen3.5-4B-Q4_K_M.gguf",
  "tokenizer_id": "Qwen/Qwen3.5-4B",
  "gguf_load": {
    "n_gpu_layers": 8,
    "n_ctx": 4096,
    "reserve_bytes": 1073741824,
    "safety_margin_bytes": 1073741824
  }
}
```

POST /load returns a job ID, not a readiness guarantee. Poll /jobs/{job_id}
until completed or failed, then inspect /ready: check accepting_requests,
primer.model_id, effective_n_ctx, n_gpu_layers and runtime_profile. Readiness
indicates availability, not that every prompt fits. Record image/model identity,
settings and errors before running inference comparisons.

## Validation

Unit tests use synthetic memory/model metadata and mock native loading to cover
independent controls, capacity limits, unload-before-profile ordering, overrides,
failed profiling and cancellation. GPU load/reload tests remain a separate lab
acceptance requirement. PR #8 request interpretation is independent of this work.
