# syntax=docker/dockerfile:1

FROM python:3.12-slim

# Node.js and npm are required at runtime, not build time: MCP servers are
# commonly launched with `npx`, and without them every stdio server fails to
# start. curl backs the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

# Installed from PyPI rather than copied from an external image, so the build
# does not depend on a third-party tag resolving.
RUN pip install --no-cache-dir uv==0.8.17

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first, so a source-only change does not re-resolve them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY app.py example_config.json README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Runtime config lives on a volume so tools added through the UI survive
# container recreation; the previous relative path wrote into the image layer.
ENV MCP_CONFIG_PATH=/app/data/config.json \
    MCP_PROFILES_PATH=/app/data/profiles.json

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app /opt/venv
USER appuser

EXPOSE 8585

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl --fail http://localhost:8585/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py", \
     "--server.port=8585", \
     "--server.address=0.0.0.0", \
     "--server.headless=true"]
