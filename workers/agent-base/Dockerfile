# syntax=docker/dockerfile:1
# Base image for every agent execution (ARCHITECTURE.md section 10).
# Provider CLIs (Phase 4) and toolchains are added in derived images.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

RUN apt-get update \
 && apt-get install --no-install-recommends -y bash ca-certificates curl git tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 agent \
 && useradd --uid 10001 --gid agent --home-dir /home/agent --create-home --shell /bin/bash agent \
 && mkdir -p /workspace /output && chown agent:agent /workspace /output

LABEL org.hermes-orchestrator.role="agent-base"
ENV HOME=/home/agent
WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-lc", "echo 'agent-base: no command given'"]
