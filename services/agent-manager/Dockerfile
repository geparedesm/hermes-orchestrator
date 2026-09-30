# syntax=docker/dockerfile:1
# Build context: repository root.  docker build -f services/agent-manager/Dockerfile .
# The only platform service with Docker access (ARCHITECTURE.md AD-03, SECURITY_MODEL.md section 8).
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HO_SCHEMA_DIR=/app/schemas HO_CONFIG_DIR=/app/config
ARG TARGETARCH
# Official Docker Compose standalone binary, pinned by version and checksum, for project test
# environments (MASTER_SPEC section 52). It talks to Docker through the same socket.
ARG COMPOSE_VERSION=v5.5.1
ARG COMPOSE_SHA256_arm64=732e3a84c1a0f67256ce80bc2598a24546b10ca05f9faa97efceb1171ece2ef7
ARG COMPOSE_SHA256_amd64=db1889184726840f75c4f9c001048430d4f25b3be3cb084d3ddd762bc0aed576
RUN groupadd --gid 10003 manager && useradd --uid 10003 --gid manager --no-create-home --shell /usr/sbin/nologin manager \
 && apt-get update && apt-get install --no-install-recommends -y curl ca-certificates \
 && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
 && case "$arch" in arm64) file=aarch64 sum="$COMPOSE_SHA256_arm64" ;; amd64) file=x86_64 sum="$COMPOSE_SHA256_amd64" ;; *) exit 1 ;; esac \
 && curl -fsSL -o /usr/local/bin/docker-compose "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${file}" \
 && echo "$sum  /usr/local/bin/docker-compose" | sha256sum -c - && chmod 755 /usr/local/bin/docker-compose \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
 && docker-compose version
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
