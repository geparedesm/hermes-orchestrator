#!/usr/bin/env bash
# Write config/models.lock.json: the models each pinned provider CLI offers, for the Dashboard's model choice.
#
# Codex: the catalog bundled with the pinned CLI (`codex debug models --bundled`, run offline), models it lists in
# its own picker (visibility "list"), in its order. Claude Code has no catalog command; its documented aliases are
# built into the control plane (control_plane/agents.py). Run by build-images.sh; safe to run again on its own.
set -euo pipefail
cd "$(dirname "$0")/.."

# Any pinned Codex image carries the same CLI.
codex_image="$(sed -n 's/^  codex-[a-z0-9-]*: "\(sha256:[0-9a-f]*\)"$/\1/p' config/images.lock.yaml | head -n 1)"
if [[ -z "$codex_image" ]]; then
  rm -f config/models.lock.json  # no Codex image pinned: nothing to offer beyond observed models
  echo "no Codex image pinned; config/models.lock.json removed"
  exit 0
fi
tmp="$(mktemp config/.models.lock.XXXXXX)"
trap 'rm -f "$tmp"' EXIT
docker run --rm --network none -e CODEX_HOME=/tmp/codex --entrypoint sh "$codex_image" \
    -c 'mkdir -p /tmp/codex && codex debug models --bundled' 2>/dev/null \
  | python3 -c '
import json, sys
models = json.load(sys.stdin)["models"]
listed = sorted((m for m in models if m.get("visibility") == "list"), key=lambda m: m.get("priority", 1 << 30))
json.dump({"generated_by": "scripts/model-catalog.sh",
           "codex": [{"id": m["slug"], "label": m.get("display_name") or m["slug"]} for m in listed]},
          sys.stdout, indent=2)
' > "$tmp"
chmod 644 "$tmp"  # the control plane reads it as another user
mv "$tmp" config/models.lock.json
trap - EXIT
echo "config/models.lock.json: $(python3 -c 'import json; print(len(json.load(open("config/models.lock.json"))["codex"]))') codex models"
