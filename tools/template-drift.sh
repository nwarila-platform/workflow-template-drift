#!/usr/bin/env bash
# Python's standard library is used because neither Git nor POSIX text tools parse this YAML subset and lock relation together.
set -euo pipefail
die() { printf 'template-drift: error: local_helper: %s\n' "$*" >&2; exit 2; }
usage() { printf '%s\n' 'usage: tools/template-drift.sh check|sync [--fail-on error|warning]' >&2; }
[[ $# -ge 1 ]] || { usage; exit 2; }
command_name=$1; shift
[[ "$command_name" == check || "$command_name" == sync ]] || { usage; exit 2; }
fail_on=error
if [[ $# -gt 0 ]]; then
  [[ $# -eq 2 && "$1" == --fail-on && ("$2" == error || "$2" == warning) ]] || { usage; exit 2; }
  fail_on=$2
fi
root=$(git rev-parse --show-toplevel 2>/dev/null) || die 'not inside a Git repository'
cd "$root"
remote=$(git remote get-url origin 2>/dev/null) || die 'origin remote is required'
if [[ "$remote" =~ ^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$ ]]; then
  owner=${BASH_REMATCH[1]}; name=${BASH_REMATCH[2]}
elif [[ "$remote" =~ ^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$ ]]; then
  owner=${BASH_REMATCH[1]}; name=${BASH_REMATCH[2]}
elif [[ "$remote" =~ ^ssh://git@github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$ ]]; then
  owner=${BASH_REMATCH[1]}; name=${BASH_REMATCH[2]}
else
  die 'origin must be a supported GitHub URL'
fi
name=${name%.git}
[[ -n "$name" ]] || die 'origin must be a supported GitHub URL'
self="$owner/$name"
identity=.github/.config/template-drift.yaml
lock=.github/.config/template-drift.lock
[[ -f "$identity" && -f "$lock" ]] || die 'identity and lock files are required'
tmp_parent=${TMPDIR:-/tmp}
[[ "$tmp_parent" == /* ]] || tmp_parent="$root/$tmp_parent"
run_tmp=$(mktemp -d "$tmp_parent/template-drift.XXXXXX") || die 'cannot create run temporary'
cache_tmp=''; retain_run=0
cleanup() {
  [[ $retain_run -eq 1 || ! -d "$run_tmp" ]] || find "$run_tmp" -depth -delete
  [[ -z "$cache_tmp" || ! -d "$cache_tmp" ]] || find "$cache_tmp" -depth -delete
}
trap cleanup EXIT
# This strict subset rejects ambiguity that general YAML loaders would otherwise accept.
python3 - "$identity" "$lock" "$owner" "$self" >"$run_tmp/pins" <<'PY' || exit 2
import re, sys
identity, lock, owner, own = sys.argv[1:]
def fail(message):
    print(f"template-drift: error: identity_lock: {message}", file=sys.stderr); raise SystemExit(2)
def read(path):
    raw = open(path, "rb").read()
    if not raw.endswith(b"\n") or b"\r" in raw or b"\0" in raw: fail(f"{path} must use LF and end with LF")
    try: return raw.decode("ascii").splitlines()
    except UnicodeDecodeError: fail(f"{path} must be ASCII")
lines = read(identity)
if not lines or lines[0] != "templates:": fail("invalid identity grammar")
matches = [re.fullmatch(r"  - ([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)", line) for line in lines[1:]]
if not matches or any(match is None for match in matches): fail("invalid identity grammar")
raw_items = [match.group(1) for match in matches if match is not None]
templates = [item if "/" in item else f"{owner}/{item}" for item in raw_items]
keys = [item.lower() for item in templates]
if len(set(keys)) != len(keys): fail("identity templates must be unique after owner resolution")
entries = read(lock); pins = []
for line in entries:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) ([0-9a-f]{40})", line)
    if not match: fail("invalid lock grammar")
    pins.append(match.groups())
expected = [item for item in templates if item.lower() != own.lower()]
if [item for item, _ in pins] != expected: fail("lock list must exactly match resolved identity order with self omitted")
for item, oid in pins: print(item, oid)
PY
cache=${XDG_CACHE_HOME:-$HOME/.cache}/template-drift
pins="$run_tmp/pins"
if [[ "$command_name" == sync ]]; then
  : >"$run_tmp/new-lock"
  while read -r repository _; do
    oid=$(git ls-remote "https://github.com/${repository}.git" HEAD 2>/dev/null | awk 'NR == 1 {print $1}') || die "cannot resolve default branch for $repository"
    [[ "$oid" =~ ^[0-9a-f]{40}$ ]] || die "cannot resolve default branch for $repository"
    printf '%s %s\n' "$repository" "$oid" >>"$run_tmp/new-lock"
  done <"$pins"
  pins="$run_tmp/new-lock"
fi
mounts=(); arguments=(); index=0
while read -r repository oid; do
  [[ "$oid" =~ ^[0-9a-f]{40}$ ]] || die "invalid OID for $repository"
  owner_name=${repository%%/*}; repository_name=${repository#*/}; source_dir="$cache/$owner_name/$repository_name/$oid"
  if [[ ! -d "$source_dir/.git" ]]; then
    parent=${source_dir%/*}; mkdir -p "$parent" || die "cannot create cache for $repository"
    cache_tmp=$(mktemp -d "$parent/.${oid}.tmp.XXXXXX") || die "cannot create cache for $repository"
    git -C "$cache_tmp" init -q || die "cannot initialize cache for $repository"
    git -C "$cache_tmp" remote add origin "https://github.com/${repository}.git" || die "cannot initialize cache for $repository"
    git -C "$cache_tmp" fetch --quiet --depth=1 origin "$oid" || die "cannot fetch $repository@$oid"
    git -C "$cache_tmp" checkout --quiet --detach FETCH_HEAD || die "cannot check out $repository@$oid"
    [[ $(git -C "$cache_tmp" rev-parse HEAD) == "$oid" ]] || die "checkout mismatch for $repository"
    chmod -R a+rX,a-w "$cache_tmp" || die "cannot protect cache for $repository"
    mv "$cache_tmp" "$source_dir" || die "cannot install cache for $repository"; cache_tmp=''
  fi
  [[ $(git -C "$source_dir" rev-parse HEAD) == "$oid" ]] || die "cache mismatch for $repository"
  mounts+=(--volume "$source_dir:/templates/$index:ro")
  arguments+=(--template "$repository=/templates/$index")
  index=$((index + 1))
done <"$pins"
[[ $index -gt 0 ]] || die 'no external templates remain after self-skip'
if command -v podman >/dev/null 2>&1; then runtime=podman; user_args=(--userns=keep-id --user "$(id -u):$(id -g)");
elif command -v docker >/dev/null 2>&1; then runtime=docker; user_args=(--user "$(id -u):$(id -g)");
else die 'podman or docker is required'; fi
# The local helper uses the release tag; signature-verified CI is the authority for its immutable digest.
image=ghcr.io/nwarila-platform/workflow-template-drift:2.0.0
"$runtime" pull --quiet "$image" >/dev/null || die 'image pull failed'
format='text'; [[ "$command_name" == sync ]] && format='patch'
status=0
"$runtime" run --rm --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  "${user_args[@]}" --volume "$root:/workspace:ro" "${mounts[@]}" "$image" \
  --workspace /workspace "${arguments[@]}" --fail-on "$fail_on" --format "$format" \
  >"$run_tmp/output" || status=$?
[[ $status -le 2 ]] || die "unexpected container status $status"
if [[ "$command_name" == check ]]; then cat "$run_tmp/output"; exit "$status"; fi
[[ $status -ne 2 ]] || die 'checker returned a tool error'
patch="$run_tmp/output"
nearest_directory() {
  local candidate=${1%/*} parent
  [[ "$candidate" != "$1" ]] || candidate=.
  while [[ ! -d "$candidate" ]]; do
    parent=${candidate%/*}; [[ "$parent" != "$candidate" ]] || parent=.; candidate=$parent
  done
  printf '%s\n' "$candidate"
}
require_path_writable() {
  local path=$1 ancestor
  ancestor=$(nearest_directory "$path")
  [[ -w "$ancestor" && -x "$ancestor" ]] || die "not writable: $path; nothing was changed"
  [[ ! -f "$path" || -w "$path" ]] || die "not writable: $path; nothing was changed"
}
if [[ -s "$patch" ]]; then
  git apply --check -- "$patch" >/dev/null 2>&1 || die 'patch does not apply; tree was not changed'
  git apply --numstat -z -- "$patch" >"$run_tmp/numstat" || die 'cannot enumerate patch paths; tree was not changed'
  : >"$run_tmp/paths"
  while IFS= read -r -d '' record; do
    path=${record#*$'\t'}; path=${path#*$'\t'}
    printf '%s\0' "$path" >>"$run_tmp/paths"
    require_path_writable "$path"
  done <"$run_tmp/numstat"
fi
lock_ancestor=$(nearest_directory "$lock")
[[ -w "$lock_ancestor" && -x "$lock_ancestor" ]] || die "not writable: $lock; nothing was changed"
lock_tmp=$(mktemp "${lock%/*}/.template-drift.lock.XXXXXX") || die 'cannot create lock temporary'
if ! cp -- "$pins" "$lock_tmp" || ! chmod 0644 "$lock_tmp"; then
  rm -f -- "$lock_tmp"; die 'cannot prepare lock temporary'
fi
if [[ -s "$patch" ]] && ! git apply -- "$patch" 2>"$run_tmp/apply.stderr"; then
  rm -f -- "$lock_tmp"; retain_run=1
  die "patch application failed; recovery patch retained at $patch; follow README recovery"
fi
if ! mv -f -- "$lock_tmp" "$lock" 2>/dev/null; then
  rm -f -- "$lock_tmp"
  if [[ -s "$patch" ]] && ! git apply -R -- "$patch" >/dev/null 2>&1; then
    die 'lock write failed; patch revert failed'
  fi
  die 'lock write failed; patch reverted'
fi
printf '%s\n' 'template-drift sync changed:'
git diff --stat -- .
