# ---------------------------------------------------------------------------
# Stage 1 — build the React dashboard.
# Node lives only in this stage; it never touches the host.
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /build

# Dependency layer caches independently of source changes.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install

COPY frontend/ ./
RUN npm run build


# ---------------------------------------------------------------------------
# Stage 2 — Python runtime.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

# curl is used by the healthcheck fallback; tzdata keeps UTC honest.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so source edits do not bust the layer.
# Dev extras (pytest/ruff/mypy) are included so tests and linters run inside
# the container — you never need a Python toolchain on the host.
COPY backend/pyproject.toml ./pyproject.toml
RUN pip install --upgrade pip && pip install '.[dev]'

COPY backend/app ./app
COPY backend/tests ./tests
COPY scripts ./scripts
# The shipped defaults. `config.yaml` is bind-mounted over the top at runtime
# and is gitignored, so this is the only copy of the safe posture the image
# has — and the one test_config.py asserts against inside the container.
COPY config.example.yaml ./config.example.yaml
COPY --from=frontend /build/dist ./frontend

# Signature timestamps are validated server-side, so run as a normal user but
# keep the clock in UTC.
RUN useradd --create-home --uid 10001 copilot \
    && chown -R copilot:copilot /app
USER copilot

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
