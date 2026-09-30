# syntax=docker/dockerfile:1
# Build context: repository root.  docker build -f services/control-plane/Dockerfile .
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HO_SCHEMA_DIR=/app/schemas \
    HO_CONFIG_DIR=/app/config \
    HO_MIGRATIONS_DIR=/app/migrations

# The artifact volume inherits this ownership when Docker first creates it.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /var/lib/ho/artifacts && chown 10001:10001 /var/lib/ho/artifacts

COPY requirements.lock /tmp/requirements.lock
COPY packages/ho_core /src/ho_core
COPY services/control-plane /src/control-plane
RUN pip install -c /tmp/requirements.lock /src/ho_core /src/control-plane && rm -rf /src /tmp/requirements.lock

COPY schemas /app/schemas
COPY migrations /app/migrations
COPY config /app/config

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2)"]
CMD ["control-plane"]
