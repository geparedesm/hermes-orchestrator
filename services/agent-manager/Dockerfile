# syntax=docker/dockerfile:1
# Build context: repository root.  docker build -f services/agent-manager/Dockerfile .
# The only platform service with Docker access (ARCHITECTURE.md AD-03, SECURITY_MODEL.md section 8).
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HO_SCHEMA_DIR=/app/schemas HO_CONFIG_DIR=/app/config
RUN groupadd --gid 10003 manager && useradd --uid 10003 --gid manager --no-create-home --shell /usr/sbin/nologin manager
COPY requirements.lock /tmp/requirements.lock
COPY packages/ho_core /src/ho_core
COPY services/agent-manager /src/agent-manager
RUN pip install -c /tmp/requirements.lock /src/ho_core /src/agent-manager && rm -rf /src /tmp/requirements.lock
COPY schemas /app/schemas
COPY config /app/config
# Non-root; access to the Docker socket comes from the socket's group (group_add in compose.yaml).
USER 10003:10003
EXPOSE 8082
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8082/health/live', timeout=2)"]
CMD ["agent-manager"]
