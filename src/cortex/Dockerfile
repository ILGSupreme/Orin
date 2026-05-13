FROM orin-gw:5000/primer-runtime:0.2

WORKDIR /app

COPY cortex/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY cortex /app/cortex
COPY common /app/common

EXPOSE 8080

ENTRYPOINT []
CMD ["python3", "-m", "uvicorn", "cortex.app:app", "--host", "0.0.0.0", "--port", "8080"]