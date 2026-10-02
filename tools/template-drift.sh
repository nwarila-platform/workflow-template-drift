#!/usr/bin/env bash
# Run the template-drift check on the repository you are standing in, the same way CI runs it:
# fetch every template at the commit the lock file pins, then run the released container with no
# network access and read-only mounts. Nothing in the repository is changed.
#
# CI also verifies the image's signature and digest before running it. This local run does not:
# it is a convenience before pushing, and CI remains the gate.
#
# Usage: tools/template-drift.sh [--fail-on warning]
# Needs: Git, and Podman or Docker.
set -euo pipefail

fail() {
  echo "template-drift: error: $1" >&2
  exit 2
}

repository=$(git rev-parse --show-toplevel)
lock="$repository/.github/.config/template-drift.lock"
image="ghcr.io/nwarila-platform/workflow-template-drift:$(cat "$(dirname "$0")/../VERSION")"
[[ -f "$lock" ]] || fail "$lock is missing"

# The container runs as the calling user so that it can read the checkout whatever its file modes.
if command -v podman >/dev/null; then
  run=(podman run --userns=keep-id)
elif command -v docker >/dev/null; then
  run=(docker run)
else
  fail "Podman or Docker is required"
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# Each line of the lock file is "owner/repo commit". Its shape is checked before Git sees it.
# The "|| [[ -n ... ]]" keeps a last line that does not end with a newline.
mounts=()
templates=()
index=0
while read -r template commit || [[ -n "$template" ]]; do
  [[ "$template $commit" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\ [0-9a-f]{40}$ ]] \
    || fail "$lock: expected 'owner/repo commit', found '$template $commit'"
  git init --quiet "$work/$index"
  git -C "$work/$index" fetch --quiet --depth=1 "https://github.com/$template.git" "$commit"
  git -C "$work/$index" checkout --quiet --detach FETCH_HEAD
  mounts+=(--volume "$work/$index:/templates/$index:ro")
  templates+=(--template "$template=/templates/$index")
  index=$((index + 1))
done <"$lock"
[[ $index -gt 0 ]] || fail "$lock names no templates"

"${run[@]}" --rm --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --user "$(id -u):$(id -g)" --volume "$repository:/workspace:ro" "${mounts[@]}" \
  "$image" --workspace /workspace "${templates[@]}" "$@"
