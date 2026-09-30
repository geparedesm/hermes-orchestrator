# syntax=docker/dockerfile:1
# Toolchain profile "python" (MASTER_SPEC section 18): Debian's Python 3, venv, and pip.
# Composable: BASE is agent-base or another toolchain image.
ARG BASE
FROM ${BASE}
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y python3 python3-venv python3-pip python-is-python3 \
 && rm -rf /var/lib/apt/lists/* \
 && python --version
# Projects install into a virtual environment inside the workspace, never into the image.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
LABEL org.hermes-orchestrator.toolchain.python="true"
USER 10001:10001
