# syntax=docker/dockerfile:1
# Base image for every execution (ARCHITECTURE.md section 10). This is also the
# "generic" toolchain. Toolchains (workers/toolchains) and provider CLIs
# (workers/providers) are added in derived images.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

RUN apt-get update \
 && apt-get install --no-install-recommends -y bash ca-certificates curl git tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 agent \
 && useradd --uid 10001 --gid agent --home-dir /home/agent --create-home --shell /bin/bash agent \
 && mkdir -p /workspace /output /opt/ho/bin /run/ho-input /run/ho-sessions /run/ho-credentials/claude /run/ho-credentials/codex \
            /cache/pip /cache/npm /cache/gradle /cache/pub /cache/composer \
 && chown agent:agent /workspace /output /run/ho-input /run/ho-sessions /run/ho-credentials/claude /run/ho-credentials/codex \
                      /cache/pip /cache/npm /cache/gradle /cache/pub /cache/composer \
 && chmod 700 /run/ho-credentials/claude /run/ho-credentials/codex
# Mount points owned by the worker user: Docker copies this ownership into a new, empty
# named volume the first time it is mounted (output, input, session, and dependency-cache volumes).
COPY --chmod=755 ho-wait-secrets ho-verify /opt/ho/bin/

LABEL org.hermes-orchestrator.role="agent-base" org.hermes-orchestrator.toolchains="generic"
ENV HOME=/home/agent
WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-lc", "echo 'agent-base: no command given'"]
