#!/usr/bin/env bash
# Image tests: run the bundled example through a built image with the same arguments and the same
# container restrictions as the organization's runner, and compare the report byte for byte.
# ci.yaml runs this file on each architecture's build, and publish.yaml runs it again on each
# published image before that image is promoted.
#
# Usage: tests/image.sh <image> <platform>
# Set CONTAINER_RUNTIME=podman to use Podman instead of Docker.
set -euo pipefail
image=$1
platform=$2
runtime=${CONTAINER_RUNTIME:-docker}
cd "$(dirname "$0")/.."

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The container runs as an unprivileged user, so it is given a world-readable copy of the example,
# just as the runner makes a checkout world-readable before mounting it.
cp -R example "$work/example"
chmod -R a+rX "$work/example"

# Run the image, keeping its report, its error output and its exit status. A status other than 0
# is a result to compare here, not a failure of this script.
run() {
  status=0
  "$runtime" run --rm --platform "$platform" --network=none --read-only --cap-drop=ALL \
    --security-opt=no-new-privileges "$@" >"$work/report" 2>"$work/errors" || status=$?
}

fail() {
  echo "image test failed: $1 (status $status)" >&2
  cat "$work/errors" >&2
  exit 1
}

# The example repository has drifted from its template: status 1 and exactly the expected report.
run --volume "$work/example/repository:/workspace:ro" --volume "$work/example/template:/templates/0:ro" \
  "$image" --workspace /workspace --template example/template=/templates/0 --fail-on error --format text
[[ $status -eq 1 ]] || fail "the example should exit with status 1"
diff example/expected-report.txt "$work/report" || fail "the example's report differs from example/expected-report.txt"

# No arguments is a usage error: status 2 and no report.
run "$image"
[[ $status -eq 2 && ! -s "$work/report" ]] || fail "a usage error should exit with status 2 and print no report"

echo "image tests passed: $image on $platform"
