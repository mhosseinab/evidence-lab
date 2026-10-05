FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
COPY configs ./configs
COPY data ./data
COPY docs ./docs
COPY scripts ./scripts
COPY tests ./tests
COPY README.md ./
RUN pip install --no-cache-dir --no-deps . \
    && useradd --create-home --uid 10001 rag \
    && mkdir -p /app/artifacts /app/policies \
    && chown -R rag:rag /app/artifacts /app/policies
USER rag
EXPOSE 8000
CMD ["rag-poc", "serve", "--config", "/app/configs/runtime.yaml"]
