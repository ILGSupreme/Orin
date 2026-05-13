FROM orin-gw:5000/primer-runtime:0.2

ENV MEMORY_BASE_DIR=/data/cluster-memory

WORKDIR /app

COPY memory/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY memory /app/memory
COPY common /app/common

RUN mkdir -p /data/cluster-memory

EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "memory.app:app", "--host", "0.0.0.0", "--port", "8080"]