# =========================================================
# Builder stage
# =========================================================
FROM nvidia/cuda:12.8.1-devel-ubuntu22.04 AS builder

WORKDIR /opt/orin

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/models/huggingface
ENV TRANSFORMERS_CACHE=/models/huggingface
ENV VENV_PATH=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
    cmake \
    ninja-build \
    pkg-config \
    git \
    curl \
    libopenblas-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv ${VENV_PATH}
ENV PATH="${VENV_PATH}/bin:${PATH}"

RUN pip install --upgrade pip setuptools wheel

# CUDA stub fix for build-time linking
RUN if [ -f /usr/local/cuda/lib64/stubs/libcuda.so ]; then \
        ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1; \
    fi

ENV LIBRARY_PATH=/usr/local/cuda/lib64/stubs
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64/stubs:/usr/local/cuda/lib64

# Install GGUF backend support
RUN CMAKE_ARGS="-DGGML_CUDA=on" FORCE_CMAKE=1 \
    pip install --no-cache-dir --upgrade --force-reinstall llama-cpp-python

# Install vLLM
RUN pip install --no-cache-dir vllm

# Optional place for shared Python deps across llm/cortex/tool later
# COPY docker/unified-llm/requirements.txt /tmp/requirements.txt
# RUN pip install --no-cache-dir -r /tmp/requirements.txt


# =========================================================
# Runtime stage
# =========================================================
FROM nvidia/cuda:12.8.1-runtime-ubuntu22.04

WORKDIR /opt/orin

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/models/huggingface
ENV TRANSFORMERS_CACHE=/models/huggingface
ENV VENV_PATH=/opt/venv
ENV PATH="${VENV_PATH}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    libopenblas0 \
    libgomp1 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Optional default runtime envs for Primer-capable services
ENV PRIMER_BACKEND=
ENV PRIMER_MODEL_ID=
ENV PRIMER_MODEL_PATH=
ENV PRIMER_TOKENIZER_ID=
ENV PRIMER_MAX_MODEL_LEN=4096
ENV PRIMER_MAX_NEW_TOKENS=1400
ENV PRIMER_TEMPERATURE=0.1
ENV PRIMER_TOP_P=0.95
ENV PRIMER_GPU_MEMORY_UTILIZATION=0.50
ENV PRIMER_TENSOR_PARALLEL_SIZE=1
ENV PRIMER_N_GPU_LAYERS=-1
ENV PRIMER_N_THREADS=6
ENV PRIMER_N_BATCH=512