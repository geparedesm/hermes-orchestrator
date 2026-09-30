# syntax=docker/dockerfile:1
# Build context: repository root.  docker build -f services/git-service/Dockerfile .
# The only holder of GitHub credentials and the only writer of project repositories
# (ARCHITECTURE.md AD-07, SECURITY_MODEL.md section 7.2).
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

ARG TARGETARCH
# Official GitHub CLI release, pinned by version and checksum (github.com/cli/cli releases).
ARG GH_VERSION=2.101.0
ARG GH_SHA256_arm64=b57e8063f18862647c9d22727c32e9da1b963f8bf9db648fe123a6975695640f
ARG GH_SHA256_amd64=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HO_SCHEMA_DIR=/app/schemas \
    GH_CONFIG_DIR=/var/lib/ho/gh

RUN apt-get update \
 && apt-get install --no-install-recommends -y git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
 && case "$arch" in arm64) sum="$GH_SHA256_arm64" ;; amd64) sum="$GH_SHA256_amd64" ;; *) echo "unsupported $arch"; exit 1 ;; esac \
 && curl -fsSL -o /tmp/gh.tar.gz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
 && echo "$sum  /tmp/gh.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/gh.tar.gz -C /tmp && mv "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh \
 && rm -rf /tmp/gh* && gh --version \
 && apt-get purge -y curl && apt-get autoremove -y \
 && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /var/lib/ho/gh && chown app:app /var/lib/ho/gh && chmod 700 /var/lib/ho/gh

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
