# syntax=docker/dockerfile:1
# Toolchain profile "java" (MASTER_SPEC section 18): OpenJDK 17 and Maven.
# Gradle projects use their own wrapper (./gradlew).
# Composable: BASE is agent-base or another toolchain image.
ARG BASE
FROM ${BASE}
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y openjdk-17-jdk-headless maven \
 && rm -rf /var/lib/apt/lists/* \
 && java -version && mvn --version
LABEL org.hermes-orchestrator.toolchain.java="true"
USER 10001:10001
