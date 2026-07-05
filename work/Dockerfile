# MeshAI Dockerfile
# LLM-powered Meshtastic assistant
#
# Build: docker build -t meshai .
# Run:   docker run -d --name meshai \
#          --device=/dev/ttyUSB0 \
#          -p 7681:7681 \
#          -v meshai_data:/data \
#          meshai

# ── Stage 1: Build frontend ──
FROM node:20-alpine AS frontend
WORKDIR /build
COPY dashboard-frontend/ ./dashboard-frontend/
WORKDIR /build/dashboard-frontend
RUN npm ci && npm run build
# Output lands at /build/meshai/dashboard/static/ (via vite outDir)

# ── Stage 2: Python runtime ──
FROM python:3.11-slim-bookworm

LABEL maintainer="K7ZVX <matt@echo6.co>"
LABEL description="MeshAI - LLM-powered Meshtastic assistant"
LABEL version="0.1.0"
LABEL org.opencontainers.image.source=https://github.com/zvx-echo6/meshai
LABEL org.opencontainers.image.description="MeshAI - LLM-powered Meshtastic assistant"
LABEL org.opencontainers.image.licenses=MIT

# Build arguments
ARG UID=1000
ARG GID=1000

# Environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    # For serial communication
    udev \
    # For health checks
    curl \
    # For process management
    procps \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -g ${GID} meshai && \
    useradd -u ${UID} -g ${GID} -m -s /bin/bash meshai && \
    # Add to dialout group for serial access
    usermod -aG dialout meshai

# Create directories
RUN mkdir -p /app /data && \
    chown -R meshai:meshai /app /data

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Pre-download embedding model for hybrid search
RUN python3 -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

# Copy application code
COPY --chown=meshai:meshai meshai/ ./meshai/
# Overwrite with freshly built frontend assets from stage 1
COPY --from=frontend --chown=meshai:meshai /build/meshai/dashboard/static/ ./meshai/dashboard/static/
COPY --chown=meshai:meshai pyproject.toml .
COPY --chown=meshai:meshai README.md .
COPY --chown=meshai:meshai config.example.yaml .
COPY --chown=meshai:meshai docker-entrypoint.sh .

# Install the package
RUN pip install --no-cache-dir -e .

# Switch to non-root user
USER meshai

# Data volume mount point
VOLUME ["/data"]

# Expose dashboard port
EXPOSE 8080

# Health check - verify bot process is alive via PID file
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD test -f /tmp/meshai.pid && kill -0 $(cat /tmp/meshai.pid) 2>/dev/null && [ "$(cat /tmp/meshai.link 2>/dev/null)" = up ] || exit 1

# Entrypoint writes default config on first run, then starts the bot
ENTRYPOINT ["/app/docker-entrypoint.sh"]
