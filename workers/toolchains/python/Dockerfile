# syntax=docker/dockerfile:1
# Toolchain profile "python" (MASTER_SPEC section 18): Debian's Python 3, venv, and pip.
# Composable: BASE is agent-base or another toolchain image.
ARG BASE
FROM ${BASE}
ARG PYTHON_QUALITY_PYTHON
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y python3 python3-venv python3-pip python-is-python3 \
 && rm -rf /var/lib/apt/lists/* \
 && python --version
# Lint, typecheck and security tools for the Quality Gate (pinned in workers/versions.env), in their own virtualenv:
# only their commands are on the PATH, never mixed with a project's dependencies.
# Composed on the odoo toolchain, its own quality virtualenv (the same versions) is kept.
RUN test -n "$PYTHON_QUALITY_PYTHON" \
 && if [ ! -x /opt/ho/quality/bin/mypy ]; then \
      python3 -m venv /opt/ho/quality \
      && /opt/ho/quality/bin/pip install --no-cache-dir --disable-pip-version-check $PYTHON_QUALITY_PYTHON; \
    fi \
 && for tool in ruff mypy bandit; do ln -sf /opt/ho/quality/bin/$tool /usr/local/bin/$tool; done \
 && ruff --version && mypy --version && bandit --version
# Projects install into a virtual environment inside the workspace, never into the image.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
LABEL org.hermes-orchestrator.toolchain.python="true"
USER 10001:10001
