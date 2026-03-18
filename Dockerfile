FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (libpcre2 required by semgrep's native osemgrep binary)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    libpcre2-8-0 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (setuptools first — pkg_resources required by semgrep deps)
COPY requirements.txt .
RUN pip install --no-cache-dir setuptools && \
    pip install --no-cache-dir -r requirements.txt && \
    semgrep --version

# Copy application code
COPY . .

# Create non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
ENV SEMGREP_VERSION_CACHE_PATH=/tmp/.semgrep_version
ENV SEMGREP_SEND_METRICS=off
USER appuser

EXPOSE 8000
