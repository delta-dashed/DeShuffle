# Build stage
FROM python:3.12-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Runtime stage
FROM python:3.12-slim

# Optional temporary archive importer. Authentication belongs in the /data volume.
ARG INSTALL_CODEX=false
ARG CODEX_VERSION=0.152.1
RUN if [ "$INSTALL_CODEX" = "true" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
      && npm install --global "@openai/codex@${CODEX_VERSION}" \
      && npm cache clean --force \
      && rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /app

# Create non-root user
RUN useradd -m -u 1000 botuser
RUN mkdir -p /data && chown botuser:botuser /data

# Copy installed packages from builder
COPY --from=builder /root/.local /home/botuser/.local

# Copy application files
COPY --chown=botuser:botuser Shuffle.py ./Shuffle.py
COPY --chown=botuser:botuser bookclub ./bookclub
COPY --chown=botuser:botuser questions.json ./questions.json

# Switch to non-root user
USER botuser

# Add local bin to PATH
ENV PATH=/home/botuser/.local/bin:$PATH
ENV RESHUFFLE_DATA_DIR=/data

# Run the bot
CMD ["python", "-u", "Shuffle.py"]
