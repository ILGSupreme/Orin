FROM orin-gw:5000/primer-runtime:0.2

WORKDIR /app

COPY llm/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY llm /app/llm
COPY common /app/common

EXPOSE 8080

CMD ["python3", "-m", "uvicorn", "llm.app:app", "--host", "0.0.0.0", "--port", "8080"]