# MeshAI Dockerfile
# LLM-powered Meshtastic assistant
#
# Build: docker build -t meshai .
# Run:   docker run -d --name meshai \
#          --device=/dev/ttyUSB0 \
#          -p 7681:7681 \
#          -v meshai_data:/data \
#          meshai

FROM python:3.11-slim-bookworm

LABEL maintainer="K7ZVX <matt@echo6.co>"
LABEL description="MeshAI - LLM-powered Meshtastic assistant"
LABEL version="0.1.0"

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
    && rm -rf /var/lib/apt/lists/* \
    # Install ttyd for web-based config interface
    && curl -sL https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64 -o /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd

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

# Copy application code
COPY --chown=meshai:meshai meshai/ ./meshai/
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

# Expose ttyd web config port
EXPOSE 7682

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import sqlite3; sqlite3.connect('/data/conversations.db').execute('SELECT 1')" || exit 1

# Entrypoint handles config and ttyd
ENTRYPOINT ["/app/docker-entrypoint.sh"]
