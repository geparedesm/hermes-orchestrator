#!/usr/bin/env bash
# Build every platform image for the other architecture and run each one under emulation (PHASES.md
# Phase 11: ARM64/AMD64). On an Apple Silicon Mac this proves the x86-64 Linux images; on x86-64 Linux,
# run it with HO_CHECK_PLATFORM=linux/arm64. Base images are pinned to multi-architecture index digests.
#
# Usage: scripts/check-multiarch.sh            (needs Docker with buildx emulation, about 20 minutes)
set -euo pipefail
cd "$(dirname "$0")/.."
PLATFORM="${HO_CHECK_PLATFORM:-linux/amd64}"
TAG="multiarch-check"
FAILURES=0
run() {  # run <image> <command...>: prints the output's last line
  docker run --rm --platform "$PLATFORM" --network none --entrypoint "$2" "hermes-orchestrator/$1:$TAG" "${@:3}" 2>&1 | tail -1
}
check() {
  if [[ "$3" == *"$2"* ]]; then echo "  ok    $1 ($3)"; else echo "  FAIL  $1: expected '$2' in '$3'"; FAILURES=$((FAILURES + 1)); fi
}
cleanup() { docker images --format '{{.Repository}}:{{.Tag}}' | grep ":$TAG\$" | xargs docker image rm >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== services for $PLATFORM"
for service in control-plane git-service agent-manager; do
  docker build -q --platform "$PLATFORM" -t "hermes-orchestrator/$service:$TAG" -f "services/$service/Dockerfile" . >/dev/null
done
arch="$(run control-plane uname -m)"
case "$arch" in x86_64) arch=amd64 ;; aarch64) arch=arm64 ;; esac
check "control-plane runs on $PLATFORM" "${PLATFORM#linux/}" "$arch"
check "control-plane CLI" "usage: ho" \
  "$(docker run --rm --platform "$PLATFORM" --network none --entrypoint ho "hermes-orchestrator/control-plane:$TAG" --help 2>&1 | head -1)"
check "git-service imports" "ok" "$(run git-service python -c 'import git_service.app; print("ok")')"
check "agent-manager imports" "ok" "$(run agent-manager python -c 'import agent_manager.app; print("ok")')"

echo "== execution images for $PLATFORM"
HO_PLATFORM="$PLATFORM" HO_VERSION="$TAG" HO_TOOLCHAINS="generic python node" HO_BUILD_BROWSER=1 scripts/build-images.sh
check "agent-base tools" "git version" "$(run agent-base git --version)"
check "python toolchain" "Python 3" "$(run runner-python python3 --version 2>/dev/null || run toolchain-python python3 --version)"
check "node toolchain" "v" "$(run toolchain-node node --version)"
check "Codex CLI" "$(grep -E '^CODEX_VERSION=' workers/versions.env | cut -d= -f2)" "$(run codex-generic codex --version)"
# QEMU cannot execute Claude Code's native binary: check the installed binary's ELF machine instead.
expected="$( [[ "$PLATFORM" == linux/amd64 ]] && echo "3e 00" || echo "b7 00")"
check "Claude Code binary built for $PLATFORM" "$expected" \
  "$(docker run --rm --platform "$PLATFORM" --network none --entrypoint od "hermes-orchestrator/claude-generic:$TAG" \
     -An -tx1 -j18 -N2 /opt/ho/claude/bin/claude 2>&1 | tr -s ' ' | sed 's/^ //')"
check "browser runner" "Version" "$(run browser-runner python3 -c 'import playwright; from importlib.metadata import version; print("Version", version("playwright"))')"
check "Hermes image (official, multi-arch)" "Hermes Agent" \
  "$(docker run --rm --platform "$PLATFORM" --network none --entrypoint hermes \
     nousresearch/hermes-agent@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7 --version 2>&1 | head -1)"

echo
if [[ $FAILURES -eq 0 ]]; then echo "multi-architecture check ($PLATFORM): all checks passed"; else echo "multi-architecture check: $FAILURES failed"; exit 1; fi
