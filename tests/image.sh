#!/usr/bin/env bash
# workflow-template-drift image tests: the determinism goldens in text and patch format, the exact usage
# error, and the image identity. ci.yaml runs them on each architecture's build and publish.yaml on each
# published child digest before promotion. Usage: tests/image.sh <image> <platform>
set -euo pipefail
image=$1
platform=$2
runtime=${CONTAINER_RUNTIME:-docker}
cd "$(dirname "$0")/.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
status=0
fail() {
  printf 'image test failed: %s (platform %s, status %s)\n' "$1" "$platform" "$status" >&2
  head -c 4096 "$work/err" >&2 || true
  exit 1
}
run() {
  status=0
  "$runtime" run --quiet --rm --platform "$platform" --network=none --read-only --cap-drop=ALL \
    --security-opt=no-new-privileges "$@" >"$work/out" 2>"$work/err" || status=$?
}
for format in text patch; do
  run -v "$PWD/fixtures/determinism/workspace:/workspace:ro" -v "$PWD/fixtures/determinism/source:/template:ro" \
    "$image" --workspace /workspace --template example/determinism=/template --config drift.json \
    --fail-on error --format "$format"
  [[ $status -eq 1 && ! -s "$work/err" ]] || fail "$format: status or stderr"
  cmp -s "$work/out" "fixtures/goldens/determinism.$format" || fail "$format: golden bytes"
done
run "$image"
[[ $status -eq 2 && ! -s "$work/out" ]] || fail 'usage: status or stdout'
printf '%s\n' 'template-drift: error: invalid_usage: invalid command-line arguments' | cmp -s - "$work/err" \
  || fail 'usage: stderr'
identity=$("$runtime" image inspect "$image" --format '{{.Config.User}} {{json .Config.Entrypoint}}')
[[ "$identity" == '65532:65532 ["/usr/bin/python3.12","-I","-X","utf8","-B","-m","workflow_template_drift"]' ]] \
  || fail "identity: $identity"
printf 'image tests passed: %s on %s\n' "$image" "$platform"
