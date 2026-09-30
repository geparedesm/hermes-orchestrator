# syntax=docker/dockerfile:1
# Toolchain profile "node" (MASTER_SPEC section 18): Node.js, npm, npx, and corepack.
# Composable: BASE is agent-base or another toolchain image.
ARG BASE
ARG NODE_IMAGE
FROM ${NODE_IMAGE} AS node

FROM ${BASE}
USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
 && node --version && npm --version
LABEL org.hermes-orchestrator.toolchain.node="true"
USER 10001:10001
