# syntax=docker/dockerfile:1
# Codex CLI worker layer (MASTER_SPEC sections 19-20). Composable: BASE is
# agent-base or a toolchain image. The CLI comes from its official npm package
# at a pinned version and runs through the package's own launcher, with a
# private Node.js runtime under /opt/ho/node (not on PATH).
ARG BASE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS cli
ARG CODEX_VERSION
RUN npm install --global --no-audit --no-fund "@openai/codex@${CODEX_VERSION}" && codex --version

FROM ${BASE}
ARG CODEX_VERSION
USER root
COPY --from=cli /usr/local/bin/node /opt/ho/node/bin/node
COPY --from=cli /usr/local/lib/node_modules/@openai /opt/ho/codex/node_modules/@openai
COPY --chmod=755 codex /usr/local/bin/codex
COPY --chmod=755 ho-agent-run ho-auth-login /opt/ho/bin/
RUN HOME=/tmp CODEX_HOME=/tmp/codex codex --version && rm -rf /tmp/codex
# CODEX_HOME is per execution (tmpfs); auth.json comes from the provider credential volume.
ENV CODEX_HOME=/home/agent/.codex
LABEL org.hermes-orchestrator.provider="codex" org.hermes-orchestrator.provider-version="${CODEX_VERSION}"
USER 10001:10001
