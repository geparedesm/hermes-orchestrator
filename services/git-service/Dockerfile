# syntax=docker/dockerfile:1
# Build context: repository root.  docker build -f services/git-service/Dockerfile .
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HO_SCHEMA_DIR=/app/schemas

RUN apt-get update \
 && apt-get install --no-install-recommends -y git \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY requirements.lock /tmp/requirements.lock
COPY packages/ho_core /src/ho_core
COPY services/git-service /src/git-service
RUN pip install -c /tmp/requirements.lock /src/ho_core /src/git-service && rm -rf /src /tmp/requirements.lock

COPY schemas /app/schemas

USER 10001:10001
EXPOSE 8081
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/live', timeout=2)"]
CMD ["git-service"]
