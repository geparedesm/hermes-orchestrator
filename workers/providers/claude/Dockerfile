# syntax=docker/dockerfile:1
# Claude Code worker layer (MASTER_SPEC sections 19-20). Composable: BASE is
# agent-base or a toolchain image. The CLI comes from its official npm package
# at a pinned version; the package ships a native binary, so no Node.js runtime
# is added to the image.
ARG BASE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS cli
ARG CLAUDE_CODE_VERSION
# CLI_SELF_CHECK=0 only for cross-architecture builds: QEMU cannot run Claude Code's native binary
# (scripts/check-multiarch.sh checks its architecture instead); native builds always run it.
ARG CLI_SELF_CHECK=1
RUN npm install --global --no-audit --no-fund "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && { [ "$CLI_SELF_CHECK" = 0 ] || /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe --version; }

FROM ${BASE}
ARG CLAUDE_CODE_VERSION
ARG CLI_SELF_CHECK=1
USER root
COPY --from=cli /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /opt/ho/claude/bin/claude
COPY --chmod=755 ho-agent-run ho-auth-login /opt/ho/bin/
RUN ln -s /opt/ho/claude/bin/claude /usr/local/bin/claude \
 && { [ "$CLI_SELF_CHECK" = 0 ] || { HOME=/tmp CLAUDE_CONFIG_DIR=/tmp/claude claude --version && rm -rf /tmp/claude; }; }
# The configuration directory is per execution (tmpfs); the subscription token comes
# from the provider credential volume. No automatic updates: versions are pinned here.
ENV CLAUDE_CONFIG_DIR=/home/agent/.claude DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
LABEL org.hermes-orchestrator.provider="claude" org.hermes-orchestrator.provider-version="${CLAUDE_CODE_VERSION}"
USER 10001:10001
