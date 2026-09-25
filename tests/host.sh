#!/usr/bin/env bash
# workflow-template-drift host tests: the release tripwires, then the 18-test harness. ci.yaml runs this file.
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(cat VERSION)
test "$version" = 2.1.0
grep -Fqx "__version__ = \"${version}\"" workflow_template_drift/__init__.py
grep -Fqx "    uses: nwarila-platform/.github/.github/workflows/run-container.yaml@49b9084988d2bb64d14f0ed2a81453a3435b935d" .github/workflows/check.yaml
grep -Fqx "      version: ${version}" .github/workflows/check.yaml
grep -Fqx "image=ghcr.io/nwarila-platform/workflow-template-drift:${version}" tools/template-drift.sh
grep -Fqx "    uses: nwarila-platform/workflow-template-drift/.github/workflows/check.yaml@<40-hex> # v${version}" README.md
grep -Fqx "      version: ${version}" README.md
test "$(git ls-files -s tools/template-drift.sh | cut -c1-6)" = 100755
python3.12 -m venv --without-pip .runtime
ln -s ../../../../workflow_template_drift .runtime/lib/python3.12/site-packages/workflow_template_drift
PATH="$PWD/.runtime/bin:$PATH" python3.12 -B -m unittest discover -s . -t . -v 2>&1 | tee .runtime/unittest.output
grep -q '^Ran 18 tests' .runtime/unittest.output
grep -q '^OK$' .runtime/unittest.output
