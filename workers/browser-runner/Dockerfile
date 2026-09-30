# syntax=docker/dockerfile:1
# Browser runner (MASTER_SPEC section 55): official Playwright image (Chromium, Firefox, WebKit)
# pinned by digest, the matching Python Playwright package, a non-root user, and the platform's
# verification scripts. No provider CLI, no personal browser profile: every run starts from an
# empty in-memory home, so no cookies, sessions, or passwords can be inherited.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd171a9ffc2b4b59532aaa6210e21014d07093120dc25528870c0b840da1f0

USER root
RUN apt-get update && apt-get install --no-install-recommends -y tini git \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 10001 agent && useradd --uid 10001 --gid agent --home-dir /home/agent --create-home --shell /bin/bash agent \
 && mkdir -p /workspace /output /opt/ho/bin /run/ho-input && chown agent:agent /workspace /output /run/ho-input \
 && pip install --no-cache-dir --break-system-packages "playwright==1.63.0" \
 && python3 -c "from importlib.metadata import version; print('playwright', version('playwright'))"
COPY --chmod=755 ho-wait-secrets ho-verify ho-browser-check /opt/ho/bin/

LABEL org.hermes-orchestrator.role="browser-runner"
ENV HOME=/home/agent PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "-lc", "echo 'browser-runner: no command given'"]
