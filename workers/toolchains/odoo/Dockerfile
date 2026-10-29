# syntax=docker/dockerfile:1
# Toolchain profile "odoo" (docs/design/child-projects.md): Odoo, pinned by date and SHA-256 in
# workers/versions.env, with its Python dependencies in /opt/odoo/venv, and `ho-odoo-test`, which installs a module
# from /workspace into a throwaway database on the test PostgreSQL service and runs its tests. Chromium (Debian's
# package) and websocket-client (ODOO_EXTRA_PYTHON) let HttpCase browser tests and tours run, offline. The Quality
# Gate's lint, security and typecheck tools (ODOO_QUALITY_PYTHON) live in /opt/ho/quality with `ho-odoo-lint` and
# `ho-odoo-security`, whose rules are in /opt/ho/share/odoo-quality.
# Composable: BASE is agent-base or another toolchain image.
ARG BASE
FROM ${BASE}
ARG ODOO_SERIES
ARG ODOO_RELEASE
ARG ODOO_SHA256
ARG ODOO_EXTRA_PYTHON
ARG ODOO_QUALITY_PYTHON
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y python3 python3-venv python3-dev python-is-python3 build-essential \
      libpq-dev libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev libjpeg-dev zlib1g-dev libffi-dev \
      postgresql-client fonts-dejavu-core \
      chromium fonts-liberation \
 && rm -rf /var/lib/apt/lists/*
# Exactly the approved release: the archive is checked against its SHA-256 before anything is extracted.
RUN test -n "$ODOO_SERIES" -a -n "$ODOO_RELEASE" -a -n "$ODOO_SHA256" \
 && curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 -o /tmp/odoo.tgz "https://nightly.odoo.com/${ODOO_SERIES}/nightly/tgz/odoo_${ODOO_SERIES}.${ODOO_RELEASE}.tar.gz" \
 && echo "${ODOO_SHA256}  /tmp/odoo.tgz" | sha256sum -c - \
 && mkdir -p /opt/odoo \
 && tar -xzf /tmp/odoo.tgz -C /opt/odoo --strip-components=1 \
 && rm /tmp/odoo.tgz \
 && cp /opt/odoo/setup/odoo /opt/odoo/odoo-bin \
 && python3 -m venv /opt/odoo/venv \
 && /opt/odoo/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /opt/odoo/requirements.txt \
 && if [ -n "$ODOO_EXTRA_PYTHON" ]; then /opt/odoo/venv/bin/pip install --no-cache-dir --disable-pip-version-check $ODOO_EXTRA_PYTHON; fi \
 && /opt/odoo/venv/bin/python /opt/odoo/odoo-bin --version
# Projects may explicitly invoke /usr/bin/python3 for their standalone tests. The pinned module extras must be
# available to that interpreter too, without exposing all of Odoo's virtualenv packages or changing the command.
RUN if [ -n "$ODOO_EXTRA_PYTHON" ]; then \
      system_packages="$(/usr/bin/python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"; \
      /opt/odoo/venv/bin/pip install --no-cache-dir --disable-pip-version-check --target="$system_packages" $ODOO_EXTRA_PYTHON; \
    fi
# Lint and security tools: their own virtualenv (never Odoo's dependencies); only their commands are on the PATH.
RUN test -n "$ODOO_QUALITY_PYTHON" \
 && python3 -m venv /opt/ho/quality \
 && /opt/ho/quality/bin/pip install --no-cache-dir --disable-pip-version-check $ODOO_QUALITY_PYTHON \
 && for tool in ruff pylint bandit mypy; do ln -s /opt/ho/quality/bin/$tool /usr/local/bin/$tool; done
COPY quality/ /opt/ho/share/odoo-quality/
COPY --chmod=755 ho-odoo-test ho-odoo-lint ho-odoo-security ho-odoo-access-check /opt/ho/bin/
# Login shells reset PATH (agent-base's /etc/ho-path brings these back); the test runner is also on the default PATH.
RUN printf '%s\n' /opt/odoo/venv/bin >> /etc/ho-path \
 && for tool in ho-odoo-test ho-odoo-lint ho-odoo-security ho-odoo-access-check; do ln -s /opt/ho/bin/$tool /usr/local/bin/$tool; done
# Test runs never write bytecode into the workspace.
ENV PYTHONDONTWRITEBYTECODE=1 HO_ODOO_HOME=/opt/odoo HO_ODOO_SERIES=${ODOO_SERIES} PATH=/opt/odoo/venv/bin:/opt/ho/bin:$PATH
LABEL org.hermes-orchestrator.toolchain.odoo="${ODOO_SERIES}.${ODOO_RELEASE}"
USER 10001:10001
