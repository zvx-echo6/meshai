FROM python:3.11-slim

LABEL maintainer="K7ZVX <matt@echo6.co>"
LABEL description="MeshAI - LLM-powered Meshtastic assistant"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -s /bin/bash meshai

# Set working directory
WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY meshai/ ./meshai/
COPY pyproject.toml .
COPY README.md .

# Install the package and fix permissions
RUN pip install --no-cache-dir -e . && \
    chown -R meshai:meshai /app

# Create data directory for config and database
RUN mkdir -p /data && chown meshai:meshai /data

# Switch to non-root user
USER meshai

# Set working directory to data for config files
WORKDIR /data

# Default command
CMD ["python", "-m", "meshai"]
