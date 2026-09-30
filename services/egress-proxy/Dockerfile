# syntax=docker/dockerfile:1
# Build context: repository root.  docker build -f services/egress-proxy/Dockerfile .
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN groupadd --gid 10002 egress && useradd --uid 10002 --gid egress --no-create-home --shell /usr/sbin/nologin egress
COPY services/egress-proxy /src/egress-proxy
RUN pip install /src/egress-proxy && rm -rf /src
LABEL org.hermes-orchestrator.role="egress-proxy"
USER 10002:10002
EXPOSE 3128
CMD ["egress-proxy"]
