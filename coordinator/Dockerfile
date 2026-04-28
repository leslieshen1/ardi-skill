FROM python:3.12-slim

WORKDIR /app

# Install build deps for any C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy package
COPY pyproject.toml ./
COPY src ./src

# Install
RUN pip install --no-cache-dir -e .

# Non-root
RUN useradd -m -u 1000 ardi && chown -R ardi:ardi /app
USER ardi

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/v1/health', timeout=3).raise_for_status()" || exit 1

ENTRYPOINT ["ardi-coordinator"]
CMD ["--config", "/app/config.toml"]
