# Third-party licenses

Orin source code is licensed under the Apache License, Version 2.0.

This file documents third-party software, models, containers, and services that
may be used with Orin. These components may have their own licenses and terms.

The Orin license does not automatically grant rights to use, redistribute, or
commercialize third-party components.

## Important distinction

Orin source code license:

```text
Apache-2.0
```

Third-party assets may use different terms, including but not limited to:

- open-source software licenses
- model licenses
- container image licenses
- NVIDIA/CUDA/Jetson terms
- Hugging Face repository licenses
- provider-specific service terms

Users are responsible for checking and complying with those terms.

## Runtime dependencies

Examples of third-party software that may be used by Orin:

| Component | Purpose | License notes |
|---|---|---|
| Kubernetes / k3s | Cluster runtime | Check upstream license |
| FastAPI | Python API framework | Check upstream license |
| Pydantic | Data validation | Check upstream license |
| llama.cpp / llama-cpp-python | GGUF model execution | Check upstream license |
| vLLM | LLM runtime backend | Check upstream license |
| Hugging Face Hub | Model discovery/download | Check upstream license and terms |
| SQLite | Local persistence | Check upstream license |
| httpx | HTTP client | Check upstream license |
| Kubernetes Python client | Kubernetes API access | Check upstream license |

This table should be updated as dependencies are added or removed.

## Container images

Orin may use or build from third-party base images, including standard Linux,
CUDA, PyTorch, vLLM, or Jetson-specific images.

Container base images may have separate licenses and redistribution terms.

Examples:

| Image family | Purpose | License notes |
|---|---|---|
| vLLM images | vLLM runtime base | Check upstream image terms |
| NVIDIA CUDA images | GPU runtime base | Check NVIDIA terms |
| NVIDIA Jetson images | Jetson runtime base | Check NVIDIA Jetson terms |
| Python/Linux images | General runtime base | Check upstream image terms |

## Models and weights

Orin can download, load, or execute third-party models.

Model weights are not part of the Orin source-code license unless explicitly
included and licensed by Nordavind.

Before using a model, check:

- model license
- commercial-use permission
- redistribution permission
- attribution requirements
- acceptable-use restrictions
- whether generated outputs have license implications
- whether the model requires gated access or authentication

Examples of model sources that may be used:

| Source | Notes |
|---|---|
| Hugging Face | Each repository may have its own license |
| GGUF repositories | Quantized files may have separate terms from the base model |
| Vendor model repositories | Check vendor-specific terms |

## Do not commit

Do not commit the following to the Orin repository:

- model weights
- Hugging Face access tokens
- private API keys
- kubeconfig files
- SSH private keys
- registry credentials
- production secrets
- proprietary NVIDIA files outside their permitted distribution model

## Updating this file

When adding a new dependency, model, or container base image, update this file
with the relevant name, purpose, and license notes.
