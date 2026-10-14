#!/usr/bin/env bash
# Linux only: let you and the platform's services share the projects root (Docker Desktop on macOS needs nothing).
#
#   sudo scripts/linux-projects-permissions.sh [projects-root]
#
# Git Service and the workers run as uid/gid 10001 inside their containers and must write in your repositories
# (workspaces under <project>/.hermes/worktrees, approved merges), while you keep editing them as yourself. This creates
# a host group with gid 10001, adds you to it, makes the projects root group-writable with new files inheriting the
# group, and sets core.sharedRepository=group in each repository. Nothing is deleted or moved. Run it again after
# cloning a repository by hand.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $(id -u) -eq 0 ]] || { echo "run with sudo" >&2; exit 2; }
person="${SUDO_USER:?run with sudo from your own account}"
root="${1:-$(sed -n 's/^HO_PROJECTS_ROOT_HOST=//p' .env 2>/dev/null)}"
[[ -n $root && -d $root ]] || { echo "projects root not found: '$root' (pass it, or set HO_PROJECTS_ROOT_HOST in .env)" >&2; exit 2; }
gid=10001
group="$(getent group "$gid" | cut -d: -f1 || true)"
if [[ -z $group ]]; then
  group=hermes-projects
  groupadd --gid "$gid" "$group"
  echo "created group $group ($gid)"
fi
if ! id -nG "$person" | tr ' ' '\n' | grep -qx "$group"; then
  usermod -aG "$group" "$person"
  echo "added $person to $group: log out and back in (or run 'newgrp $group') for it to apply to your shell"
fi
chgrp -R "$gid" "$root"
chmod -R g+rwX "$root"
find "$root" -type d -exec chmod g+s {} +
while IFS= read -r git_dir; do
  repo="${git_dir%/.git}"
  sudo -u "$person" git -C "$repo" config core.sharedRepository group
  echo "shared: $repo"
done < <(find "$root" -name .git -type d -not -path '*/.hermes/worktrees/*' -prune)
echo "done: $root is shared by $person and the platform (group $group, $gid)"
