# syntax=docker/dockerfile:1
# Toolchain profile "php" (MASTER_SPEC section 18): PHP CLI with common extensions and Composer.
# Composable: BASE is agent-base or another toolchain image.
ARG BASE
FROM ${BASE}
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y php-cli php-curl php-mbstring php-sqlite3 php-xml php-zip composer unzip \
 && rm -rf /var/lib/apt/lists/* \
 && php --version && composer --version
LABEL org.hermes-orchestrator.toolchain.php="true"
USER 10001:10001
