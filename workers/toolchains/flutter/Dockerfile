# syntax=docker/dockerfile:1
# Toolchain profile "flutter" (MASTER_SPEC section 18): Flutter SDK at a pinned stable tag.
# The official archive is x86-64 only, so the SDK is cloned at the tag and checked
# against the commit published in the official releases manifest; this works on
# both linux/amd64 and linux/arm64. Composable: BASE is agent-base or another toolchain.
ARG BASE
FROM ${BASE}
ARG FLUTTER_VERSION
ARG FLUTTER_COMMIT
USER root
RUN apt-get update \
 && apt-get install --no-install-recommends -y unzip xz-utils zip libglu1-mesa \
 && rm -rf /var/lib/apt/lists/* \
 && git clone --depth 1 --branch "${FLUTTER_VERSION}" https://github.com/flutter/flutter.git /opt/flutter \
 && test "$(git -C /opt/flutter rev-parse HEAD)" = "${FLUTTER_COMMIT}" \
 && chown -R 10001:10001 /opt/flutter
USER 10001:10001
ENV PATH=/opt/flutter/bin:$PATH FLUTTER_SUPPRESS_ANALYTICS=true
# Download the Dart SDK and engine artifacts at build time; executions have no Internet by default.
RUN HOME=/tmp/flutter-home flutter config --no-analytics >/dev/null \
 && HOME=/tmp/flutter-home flutter precache --no-android --no-ios --no-web --no-linux --no-windows --no-macos --no-fuchsia \
 && HOME=/tmp/flutter-home flutter --version \
 && rm -rf /tmp/flutter-home
# Read-only root filesystem at run time: move the SDK cache aside and run through a wrapper
# that recreates bin/cache in /tmp (see flutter-wrapper).
USER root
RUN mv /opt/flutter/bin/cache /opt/flutter-cache && ln -s /tmp/flutter-cache /opt/flutter/bin/cache
COPY --chmod=755 flutter-wrapper /opt/ho/toolchain-bin/flutter
RUN ln -s flutter /opt/ho/toolchain-bin/dart
ENV PATH=/opt/ho/toolchain-bin:$PATH
USER 10001:10001
RUN flutter --version >/dev/null
LABEL org.hermes-orchestrator.toolchain.flutter="true"
