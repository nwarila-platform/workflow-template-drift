# workflow-template-drift

Workflow container that evaluates a repository (`/workspace`) against its template repository
(`/source`) per a drift configuration that lives in the template, and emits exactly one canonical
JSON document on stdout: a `PASS` / `WARNING` / `FAIL` status, per-file findings, and a `patch`
field containing an aggregate `git apply`-able remediation for every fixable finding (empty when
no finding is fixable). The checker makes no network calls, uses no token, and performs no
filesystem writes.

Exit codes: `0` = PASS or WARNING, `1` = policy FAIL, `2` = usage, config, source, resource-limit,
I/O, or internal error (status `ERROR`, no policy verdict). Nothing is ever written to stderr on a
normal completion.

## Configuration (`version: 3`)

A check names a `mode` and either one `path` (the same repo-relative path in template and
repository) or an explicit `source` (template) and `target` (repository). `severity` is optional and
defaults to `error`. `must_be_absent` takes `path` only. `head_lines_equal` requires `head_lines`
(1 to 10000). Targets must be unique and path-disjoint: no target may be an ancestor of another.
A target component that case-insensitively equals `.git` after trailing dots are removed is forbidden.

```json
{
  "version": 3,
  "checks": [
    {"mode": "bytes_equal", "path": ".github/workflows/security.yaml"},
    {"mode": "must_exist", "path": ".github/CODEOWNERS"},
    {"mode": "head_lines_equal", "path": ".editorconfig", "head_lines": 12, "severity": "warning"},
    {"mode": "must_be_absent", "path": ".github/dependabot.yml"}
  ]
}
```

Only `version: 3` is accepted at runtime. `tools/migrate_manifest.py` converts a `NWarila/drift-gate`
manifest (`version` `"1"` or `"2"`) offline. For a valid manifest with at least one mechanically
convertible entry whose targets meet the v3 restrictions above, it prints a candidate `version: 3`
document to stdout and lists every `scaffold_starter` path that needs a policy decision on stderr. If
the input is invalid, a convertible target violates those restrictions, there is no mechanically
convertible entry, or the candidate would exceed the v3 limit of 4096 checks or 1 MiB of config bytes,
it exits `2` and prints no stdout. The no-entry error reports that it cannot produce a valid nonempty
`checks` array. The converter writes no files and is not part of the image.

## Result

One JSON document per run. Success: `{"version": 1, "status": "PASS"|"WARNING"|"FAIL",
"findings": [{"target", "mode", "severity", "kind", "fixable", "details"}, …], "patch": "…"}`.
Error: `{"version": 1, "status": "ERROR", "error": {"code", "message"[, "path"]}}`. Findings are
sorted by `target` then `mode`; the document is at most 8,388,608 bytes and ends with one newline.

Limits: config file 1 MiB, JSON depth 32, 4096 checks, 64 MiB per file, 512 MiB read in total.
Exceeding any limit is an `ERROR` with code `resource_limit`.

## Status

The checker, the offline converter, and the evidence harness are implemented. CI runs the 51-test
corpus on a host and inside the built image for `linux/amd64` and `linux/arm64`, and proves that
the determinism fixture's checker result is byte-identical on every path.

## Publication

Pushing a signed tag `vX.Y.Z` whose version equals the `VERSION` file runs
`.github/workflows/publish.yaml`. It builds both platforms into one unaliased image index pushed by
digest to `ghcr.io/nwarila-platform/workflow-template-drift`, attaches one SPDX SBOM per platform,
signs the index and both children with Sigstore keyless signing, attaches SLSA build level 3
provenance, verifies all of it anonymously (no registry credential) together with the determinism
and usage goldens on both platforms, and only then tags the verified digest `:X.Y.Z` and
`:sha-<commit>`. There is no `latest` tag. A consumer verifies a release with:

```sh
cosign verify \
  --certificate-identity "https://github.com/nwarila-platform/workflow-template-drift/.github/workflows/publish.yaml@refs/tags/vX.Y.Z" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/nwarila-platform/workflow-template-drift:X.Y.Z
slsa-verifier verify-image "ghcr.io/nwarila-platform/workflow-template-drift@$(crane digest ghcr.io/nwarila-platform/workflow-template-drift:X.Y.Z)" \
  --source-uri github.com/nwarila-platform/workflow-template-drift --source-tag vX.Y.Z
```

## Run the harness on a host (Python 3.12, git)

```sh
python3.12 -m venv --without-pip .runtime
ln -s ../../../../workflow_template_drift .runtime/lib/python3.12/site-packages/workflow_template_drift
PATH="$PWD/.runtime/bin:$PATH" python3.12 -B -m unittest discover -s . -t . -v
```

## Build and run inside the image (`docker` or `podman`)

```sh
docker build --file Containerfile --tag workflow-template-drift:local .
docker run --rm --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  -v "$PWD/fixtures/determinism/workspace:/workspace:ro" \
  -v "$PWD/fixtures/determinism/source:/source:ro" \
  workflow-template-drift:local \
  --workspace /workspace --source /source --config drift.json --fail-on error --format json
```

The mounted trees must be readable by UID 65532; a checkout made with a 007 umask is not.
