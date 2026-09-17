# workflow-template-drift

This container compares one workspace with one or more pinned public template checkouts. It makes
no network calls or writes, uses only the Python standard library, and returns one aggregate result.
Targets must be unique and path-disjoint across every template.

Text is the default format. Findings are sorted by path, mode, and template:

```text
error: path: bytes_equal/content_mismatch (template owner/repo): target content differs from the pinned template
template-drift: FAIL (1 findings, 1 fixable)
```

The final line is `template-drift: PASS`, `template-drift: WARNING (<n> findings)`, or
`template-drift: FAIL (<n> findings, <m> fixable)`. A tool error writes only
`template-drift: error: <code>: <message> (<path>)` to stderr. Exit 0 means pass or warning, exit 1
means policy failure, and exit 2 means a usage, configuration, resource, or I/O error.
`--format patch` prints only the aggregate `git apply`-able patch and is empty when no fix is possible.
Each repeatable source argument is `--template owner/repo=dir`; labels are explicit output metadata.
With `--fail-on error`, WARNING findings still appear and annotate but do not fail the gate.

The consumer calls the inherited workflow:

```yaml
name: Workflow containers
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
  pull-requests: write
jobs:
  template-drift:
    uses: nwarila-platform/workflow-template-drift/.github/workflows/check.yaml@<40-hex> # v2.0.0
```

The checker repository wraps the generic org runner:

```yaml
jobs:
  run:
    uses: nwarila-platform/.github/.github/workflows/run-container.yaml@<40-hex>
    permissions: {contents: read, pull-requests: write}
    with:
      name: template-drift
      version: 2.0.0
```

Consumers declare trusted templates and immutable inputs:

```yaml
templates:
  - nwarila-platform/.github
  - NWarila/terraform-framework-template
```

```text
nwarila-platform/.github 213fd21563f8111abe77e589b6bd75323e608ab1
NWarila/terraform-framework-template <40-hex>
```

The lock omits the consumer's own repository. On pull requests, CI takes identity and template names
from the protected base and only OIDs from the head lock. CI verifies release-tag signatures, uses the
reported digest, validates reachability and non-rewind, then runs without network and with read-only mounts.
An ownerless identity item such as `.github` resolves under the consumer repository's own organization.
Onboard with two PRs: land identity plus lock first, then add the inherited-workflow call in a second PR.

`tools/template-drift.sh check [--fail-on error|warning]` checks the current repository.
`tools/template-drift.sh sync [--fail-on error|warning]` resolves template default-branch heads, applies
the exact patch unstaged, and rewrites the lock. The helper caches exact OIDs and supports Podman or Docker.
Its image reference is a release tag; signature-verified CI is the authority for the immutable digest.
If patch application fails after a pre-check, first correct the underlying I/O or permission failure.
For each path from `git apply --numstat -z "$recovery_patch"`, run the first two commands; after all paths, run the final two:

```sh
git apply -R --check --include="$path" "$recovery_patch"
git apply -R --include="$path" "$recovery_patch" # only after that path's check succeeds
git status --short -- <paths>
rm -rf -- "$(dirname "$recovery_patch")"
```

The checked path-wise reverse restores completely applied additions, deletions, and modifications while
skipping untouched later paths; inspect the status before removing the retained run-temporary directory.

The `template-drift` pre-commit hook runs at pre-push. `template-drift-sync` is manual. A consumer copies
both entries into `.pre-commit-config.yaml` once and may then run:

```sh
pre-commit run template-drift --all-files --hook-stage pre-push
pre-commit run template-drift-sync --all-files --hook-stage manual
```

A successful sync that changes files intentionally leaves them unstaged, so pre-commit reports
`files were modified by this hook` and exits 1; inspect and commit those changes normally.

Pushing a signed `vX.Y.Z` tag matching `VERSION` retains the existing multi-platform publish, SBOM,
provenance, and keyless signing workflow. There is no `latest` tag.

Run the host harness without package installation:

```sh
python3.12 -m venv --without-pip .runtime
ln -s ../../../../workflow_template_drift .runtime/lib/python3.12/site-packages/workflow_template_drift
PATH="$PWD/.runtime/bin:$PATH" python3.12 -B -m unittest discover -s . -t . -v
```

The custom evaluator remains necessary because Git and rsync do not combine absence/existence/head-line
policies, bounded no-follow reads, multi-template target collision checks, and one deterministic patch.
The helper composes maintained Git, Python, Podman/Docker, and pre-commit around the identity/lock protocol.
