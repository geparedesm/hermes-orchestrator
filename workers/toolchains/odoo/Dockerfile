# syntax=docker/dockerfile:1
# Toolchain profile "odoo" (docs/design/child-projects.md): Odoo, pinned by date and SHA-256 in
# workers/versions.env, with its Python dependencies in /opt/odoo/venv, and `ho-odoo-test`, which installs a module
# from /workspace into a throwaway database on the test PostgreSQL service and runs its tests.
# Composable: BASE is agent-base or another toolchain image.
ARG BASE
FROM ${BASE}
ARG ODOO_SERIES
ARG ODOO_RELEASE
ARG ODOO_SHA256
ARG ODOO_EXTRA_PYTHON
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y python3 python3-venv python3-dev python-is-python3 build-essential \
      libpq-dev libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev libjpeg-dev zlib1g-dev libffi-dev \
      postgresql-client fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
# Exactly the approved release: the archive is checked against its SHA-256 before anything is extracted.
RUN test -n "$ODOO_SERIES" -a -n "$ODOO_RELEASE" -a -n "$ODOO_SHA256" \
 && curl -fsSL -o /tmp/odoo.tgz "https://nightly.odoo.com/${ODOO_SERIES}/nightly/tgz/odoo_${ODOO_SERIES}.${ODOO_RELEASE}.tar.gz" \
 && echo "${ODOO_SHA256}  /tmp/odoo.tgz" | sha256sum -c - \
 && mkdir -p /opt/odoo \
 && tar -xzf /tmp/odoo.tgz -C /opt/odoo --strip-components=1 \
 && rm /tmp/odoo.tgz \
 && cp /opt/odoo/setup/odoo /opt/odoo/odoo-bin \
 && python3 -m venv /opt/odoo/venv \
 && /opt/odoo/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /opt/odoo/requirements.txt \
 && if [ -n "$ODOO_EXTRA_PYTHON" ]; then /opt/odoo/venv/bin/pip install --no-cache-dir --disable-pip-version-check $ODOO_EXTRA_PYTHON; fi \
 && /opt/odoo/venv/bin/python /opt/odoo/odoo-bin --version
COPY --chmod=755 ho-odoo-test /opt/ho/bin/ho-odoo-test
# Test runs never write bytecode into the workspace.
ENV PYTHONDONTWRITEBYTECODE=1 HO_ODOO_HOME=/opt/odoo PATH=/opt/odoo/venv/bin:/opt/ho/bin:$PATH
LABEL org.hermes-orchestrator.toolchain.odoo="${ODOO_SERIES}.${ODOO_RELEASE}"
USER 10001:10001
