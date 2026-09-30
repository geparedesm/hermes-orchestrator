#!/usr/bin/env bash
# Provider login bootstrap (MASTER_SPEC section 20, SECURITY_MODEL.md section 7.1).
#
#   scripts/auth-login.sh claude [identity]     (make auth-claude IDENTITY=default)
#   scripts/auth-login.sh codex  [identity]     (make auth-codex  IDENTITY=default)
#
# Creates the credential volume cred-<provider>-<identity> and runs the provider's
# documented interactive login in a throwaway container from the pinned worker
# image. Only that volume is mounted; no project, no Docker socket, no model task.
# Then it tells the control plane the login is ready, which resumes tasks that
# were waiting in AUTH_REQUIRED.
set -euo pipefail
cd "$(dirname "$0")/.."
provider="${1:?usage: $0 claude|codex [identity]}"
identity="${2:-default}"
[[ $provider == claude || $provider == codex ]] || { echo "provider must be claude or codex" >&2; exit 2; }
[[ $identity =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "invalid identity" >&2; exit 2; }
[[ -f config/images.lock.yaml ]] || { echo "run 'make images' first" >&2; exit 2; }
image="$(awk -v n="  $provider-generic:" '$0 ~ "^"n {gsub(/"/, "", $2); print $2}' config/images.lock.yaml)"
[[ -n $image ]] || { echo "$provider-generic is not in config/images.lock.yaml; run 'make images'" >&2; exit 2; }
volume="cred-$provider-$identity"

if ! docker volume inspect "$volume" >/dev/null 2>&1; then
  docker volume create --label "ho.credential=$provider/$identity" "$volume" >/dev/null
  echo "created credential volume $volume"
fi
# The worker user (UID 10001) owns the volume; nobody else in the container can read it.
docker run --rm --user 0 --network none --entrypoint /bin/sh -v "$volume:/c" "$image" \
  -c 'chown 10001:10001 /c && chmod 700 /c' >/dev/null

hardening=(--read-only --cap-drop ALL --security-opt no-new-privileges --user 10001:10001
           --tmpfs /tmp:size=64m --tmpfs /home/agent:size=128m,uid=10001,gid=10001)
docker run --rm -it "${hardening[@]}" -v "$volume:/run/ho-credentials/$provider" \
  --entrypoint /opt/ho/bin/ho-auth-login "$image"

echo
if docker compose ps --status running control-plane 2>/dev/null | grep -q control-plane; then
  docker compose exec -T control-plane ho auth ready "$provider" --identity "$identity"
else
  echo "The platform is not running. After 'make up', confirm the login with:"
  echo "  docker compose exec control-plane ho auth ready $provider --identity $identity"
fi
