FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (libpcre2 required by semgrep's native osemgrep binary)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    libpcre2-8-0 \
    && rm -rf /var/lib/apt/lists/*

# Use a virtual environment — guarantees setuptools/pkg_resources are always present
# and isolated from any system-level pip conflicts
COPY requirements.txt .
RUN python -m venv /venv && \
    /venv/bin/pip install --no-cache-dir --upgrade pip && \
    /venv/bin/pip install --no-cache-dir "setuptools<70" wheel && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt && \
    /venv/bin/semgrep --version

ENV PATH="/venv/bin:$PATH"

# Copy application code
COPY . .

# Create non-root user
# /repos holds the bot's branch snapshots for agentic review; created here so the
# named volume mounted over it starts out owned by appuser
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app && \
    mkdir -p /repos && chown appuser:appuser /repos
ENV SEMGREP_VERSION_CACHE_PATH=/tmp/.semgrep_version
ENV SEMGREP_SEND_METRICS=off
USER appuser

EXPOSE 8000
