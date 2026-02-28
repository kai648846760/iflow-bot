# ============================================================
# iflow-bot Docker Image
# Python 3.12 + Node.js 22 (for iflow CLI)
# ============================================================

FROM python:3.12-slim

# Install system dependencies + Node.js 22
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    git \
    tini \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Verify Node.js
RUN node --version && npm --version

# Install iflow CLI globally
RUN npm install -g @iflow-ai/iflow-cli@latest

# Install uv (fast Python package manager)
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy dependency files first (for layer caching)
COPY pyproject.toml uv.lock ./

# Install Python dependencies
RUN uv sync --frozen --no-dev 2>/dev/null || pip install --no-cache-dir .

# Copy application source
COPY iflow_bot/ ./iflow_bot/

# Copy entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create necessary directories
RUN mkdir -p /root/.iflow-bot/workspace \
             /root/.iflow-bot/data/cron \
             /root/.iflow-bot/media \
             /root/.iflow

# Persistent data volumes
VOLUME ["/root/.iflow-bot", "/root/.iflow"]

# Environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_ENV=production

# Health check
HEALTHCHECK --interval=60s --timeout=15s --start-period=30s --retries=3 \
    CMD iflow-bot status || exit 1

# Use tini as PID 1 for proper signal handling
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
CMD ["gateway", "run"]
