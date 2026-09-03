FROM python:3.13-slim

WORKDIR /workspace/pinchana-coub

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pinchana-core/pyproject.toml pinchana-core/uv.lock pinchana-core/README.md ../pinchana-core/
RUN mkdir -p ../pinchana-core/src
COPY pinchana-core/src ../pinchana-core/src

COPY pinchana-coub/pyproject.toml pinchana-coub/uv.lock pinchana-coub/README.md ./
RUN uv sync --frozen --no-install-project

COPY pinchana-coub/src ./src
RUN uv sync --frozen

RUN mkdir -p /app/cache
ENV CACHE_PATH=/app/cache
ENV CACHE_MAX_SIZE_GB=10.0
ENV COUB_FFMPEG_TIMEOUT_SECONDS=300

EXPOSE 8090
CMD ["uv", "run", "uvicorn", "pinchana_coub.main:app", "--host", "0.0.0.0", "--port", "8090"]

