# =============================================================================
# Runtime image. Multi-stage so build tooling never ships to production.
# Deploy target: Fly.io (Phase 4 — not wired up yet).
# =============================================================================

FROM python:3.12-slim AS builder

WORKDIR /build

# Build deps for any wheels that need compiling. Discarded with this stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

# Never run as root.
RUN useradd --create-home --uid 1000 triage

WORKDIR /app

COPY --from=builder /install /usr/local
COPY --chown=triage:triage src/ ./src/
COPY --chown=triage:triage db/ ./db/
COPY --chown=triage:triage scripts/ ./scripts/

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_RENDERER=json

USER triage

# Socket Mode opens an outbound WebSocket, so no port is exposed.
CMD ["python", "-m", "triage.main"]
