#!/usr/bin/env bash
# Generic local workflow-container launcher. Python 3 is required on every host, including Windows.
set -euo pipefail
# Git exports some of these to hooks; they would point the template-cache commands at the consumer.
unset GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT \
  GIT_OBJECT_DIRECTORY GIT_DIR GIT_WORK_TREE GIT_IMPLICIT_WORK_TREE GIT_GRAFT_FILE GIT_INDEX_FILE \
  GIT_NO_REPLACE_OBJECTS GIT_REPLACE_REF_BASE GIT_PREFIX GIT_SHALLOW_FILE GIT_COMMON_DIR

launcher_complete=0
run_tmp=''
cache_tmp=''
retain_run=0
cleanup() {
  status=$?
  trap - EXIT
  set +e
  [[ $retain_run -eq 1 || -z "$run_tmp" || ! -d "$run_tmp" ]] || find "$run_tmp" -depth -delete
  [[ -z "$cache_tmp" || ! -d "$cache_tmp" ]] || find "$cache_tmp" -depth -delete
  if [[ $launcher_complete -ne 1 && $status -ne 2 ]]; then status=2; fi
  exit "$status"
}
trap cleanup EXIT

die() { printf 'workflow-launcher: error: %s\n' "$*" >&2; exit 2; }
usage() { printf '%s\n' 'usage: workflow-container.sh <name> check|sync' >&2; }

[[ $# -eq 2 ]] || { usage; exit 2; }
name=$1
operation=$2
[[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ && ${#name} -le 91 ]] || die 'invalid container name'
[[ "$operation" == check || "$operation" == sync ]] || { usage; exit 2; }
python=''
for interpreter in python3 python; do
  if command -v "$interpreter" >/dev/null 2>&1 \
    && "$interpreter" -c 'import sys; raise SystemExit(0 if sys.version_info[0] == 3 else 1)' >/dev/null 2>&1; then
    python=$interpreter
    break
  fi
done
[[ -n "$python" ]] || die 'Python 3 is required; neither python3 nor python runs Python 3'

root=$(git rev-parse --show-toplevel 2>/dev/null) || die 'not inside a Git repository'
cd "$root"
remote=$(git remote get-url origin 2>/dev/null) || die 'origin remote is required'
if [[ "$remote" =~ ^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(\.git)?$ ]]; then
  owner=${BASH_REMATCH[1]}; repository=${BASH_REMATCH[2]}
elif [[ "$remote" =~ ^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(\.git)?$ ]]; then
  owner=${BASH_REMATCH[1]}; repository=${BASH_REMATCH[2]}
elif [[ "$remote" =~ ^ssh://git@github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(\.git)?$ ]]; then
  owner=${BASH_REMATCH[1]}; repository=${BASH_REMATCH[2]}
else
  die 'origin must be a supported GitHub URL'
fi
repository=${repository%.git}
[[ -n "$repository" ]] || die 'origin must be a supported GitHub URL'
self="$owner/$repository"
config_rel=".github/.config/${name}.yaml"
lock_rel=".github/.config/${name}.lock"
config="$root/$config_rel"
lock="$root/$lock_rel"
[[ -f "$config" ]] || die "missing $config_rel"

tmp_parent=${TMPDIR:-/tmp}
[[ "$tmp_parent" == /* ]] || tmp_parent="$root/$tmp_parent"
mkdir -p "$tmp_parent" || die 'cannot create temporary parent'
run_tmp=$(mktemp -d "$tmp_parent/workflow-container.XXXXXX") || die 'cannot create run temporary'
if [[ -n ${XDG_CACHE_HOME:-} ]]; then
  cache_home=$XDG_CACHE_HOME
elif [[ -n ${HOME:-} ]]; then
  cache_home=$HOME/.cache
else
  die 'XDG_CACHE_HOME and HOME are both unset or empty; cannot determine cache location'
fi

image_repository="ghcr.io/nwarila-platform/workflow-${name}"
"$python" - "$config" "$lock" "$owner" "$self" "$image_repository" \
  'workflow-launcher: error: pin_file: ' >"$run_tmp/parsed" <<'PY'
import re, sys

config, lock, owner, own, expected_image, prefix = sys.argv[1:]

def fail(message):
    print(f"{prefix}{message}", file=sys.stderr)
    raise SystemExit(2)

def read(path):
    raw = open(path, "rb").read()
    if not raw.endswith(b"\n") or b"\r" in raw or b"\0" in raw:
        fail(f"{path} must use LF and end with LF")
    try:
        return raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        fail(f"{path} must be ASCII")

def main():
    lines = read(config)
    header = re.fullmatch(
        r"# renovate: datasource=docker depName=(ghcr\.io/nwarila-platform/workflow-[a-z0-9]+(?:-[a-z0-9]+)*)",
        lines[0] if lines else "",
    )
    version = re.fullmatch(
        r"version: ((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))",
        lines[1] if len(lines) > 1 else "",
    )
    digest = re.fullmatch(r"digest: (sha256:[0-9a-f]{64})", lines[2] if len(lines) > 2 else "")
    fail_on = re.fullmatch(r"fail_on: (error|warning)", lines[3] if len(lines) > 3 else "")
    if header is None or version is None or digest is None or fail_on is None:
        fail("invalid header grammar")
    if header.group(1) != expected_image:
        fail("Renovate dependency does not match the requested image")
    remainder = lines[4:]
    templates = []
    if remainder and remainder[0] == "templates:":
        item = re.compile(r"  - ([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)")
        matches = []
        index = 1
        while index < len(remainder):
            match = item.fullmatch(remainder[index])
            if match is None:
                break
            matches.append(match)
            index += 1
        if not matches:
            fail("invalid template grammar")
        raw_items = [match.group(1) for match in matches]
        templates = [value if "/" in value else f"{owner}/{value}" for value in raw_items]
        folded = [value.lower() for value in templates]
        if len(set(folded)) != len(folded):
            fail("templates must be unique after owner resolution")
        remainder = remainder[index:]
    if remainder:
        if remainder[0] != "config:":
            fail("invalid template grammar")
        if any(line and not line.startswith("  ") for line in remainder[1:]):
            fail("invalid config grammar")
    print(f"VERSION {version.group(1)}")
    print(f"DIGEST {digest.group(1)}")
    print(f"FAIL_ON {fail_on.group(1)}")
    print(f"TEMPLATES {'true' if templates else 'false'}")
    if not templates:
        return
    entries = []
    for line in read(lock):
        match = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+) ([0-9a-f]{40})", line)
        if match is None:
            fail("invalid lock grammar")
        entries.append(match.groups())
    expected = [value for value in templates if value.lower() != own.lower()]
    if [value for value, _ in entries] != expected:
        fail("lock list must exactly match resolved template order with self omitted")
    for repository, oid in entries:
        print(f"PIN {repository} {oid}")

try:
    main()
except SystemExit:
    raise
except Exception:
    fail("cannot read pin file or lock")
PY

{
  read -r version_key version
  read -r digest_key digest
  read -r fail_on_key fail_on
  read -r templates_key templates_enabled
  : >"$run_tmp/pins"
  while IFS= read -r pin; do
    case "$pin" in
      'PIN '*) printf '%s\n' "${pin#PIN }" >>"$run_tmp/pins" ;;
      *) die 'parser output contract failed' ;;
    esac
  done
} <"$run_tmp/parsed"
[[ "$version_key" == VERSION && "$digest_key" == DIGEST && "$fail_on_key" == FAIL_ON \
   && "$templates_key" == TEMPLATES ]] || die 'parser output contract failed'

timeout_seconds=${WORKFLOW_CONTAINER_TIMEOUT_SECONDS:-300}
[[ "$timeout_seconds" =~ ^[1-9][0-9]*$ ]] || die 'WORKFLOW_CONTAINER_TIMEOUT_SECONDS must be a positive integer'
timeout_command=''
if command -v timeout >/dev/null 2>&1; then timeout_command=timeout
elif command -v gtimeout >/dev/null 2>&1; then timeout_command=gtimeout
fi
bounded() {
  if [[ -n "$timeout_command" ]]; then
    "$timeout_command" --foreground --kill-after=10 "$timeout_seconds" "$@"
  else
    "$@"
  fi
}

runtime=${WORKFLOW_CONTAINER_RUNTIME:-}
if [[ -n "$runtime" ]]; then
  [[ "$runtime" == podman || "$runtime" == docker ]] || die 'WORKFLOW_CONTAINER_RUNTIME must be podman or docker'
  command -v "$runtime" >/dev/null 2>&1 || die "$runtime is not installed"
elif command -v podman >/dev/null 2>&1; then
  runtime=podman
elif command -v docker >/dev/null 2>&1; then
  runtime=docker
else
  die 'podman or docker is required'
fi

host_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"
  else printf '%s\n' "$1"
  fi
}
runtime_call() {
  if command -v cygpath >/dev/null 2>&1; then MSYS_NO_PATHCONV=1 "$runtime" "$@"
  else "$runtime" "$@"
  fi
}
bounded_runtime() {
  if command -v cygpath >/dev/null 2>&1; then MSYS_NO_PATHCONV=1 bounded "$runtime" "$@"
  else bounded "$runtime" "$@"
  fi
}

image_ref="${image_repository}@${digest}"
if bounded_runtime image inspect "$image_ref" >/dev/null 2>&1; then
  :
else
  if ! bounded_runtime pull --quiet "$image_ref" >/dev/null; then
    die "image is not cached and the bounded pull failed: $image_ref"
  fi
fi

verification_cache=$cache_home/workflow-containers/verified
verification_receipt="$verification_cache/${digest#sha256:}.${name}"
identity="https://github.com/nwarila-platform/workflow-${name}/.github/workflows/publish.yaml@refs/tags/v${version}"
if command -v cosign >/dev/null 2>&1; then
  verified=false
  if [[ -f "$verification_receipt" ]] \
     && grep -Fqx "identity=$identity" "$verification_receipt" \
     && grep -Fqx "digest=$digest" "$verification_receipt"; then
    verified=true
  fi
  if [[ "$verified" != true ]]; then
    if ! bounded cosign verify --certificate-identity "$identity" \
      --certificate-oidc-issuer https://token.actions.githubusercontent.com -o json "$image_ref" \
      >"$run_tmp/cosign.json"; then
      die 'cosign verification failed'
    fi
    observed=$("$python" - "$run_tmp/cosign.json" <<'PY'
import json, re, sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        result = json.load(source)
    observed = result[0]["critical"]["image"]["docker-manifest-digest"]
    if not isinstance(observed, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", observed) is None:
        raise ValueError
except (IndexError, KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
    raise SystemExit(2)
print(observed)
PY
    ) \
      || die 'cosign result did not contain a valid digest'
    [[ "$observed" == "$digest" ]] || die 'verified image digest does not match the pin'
    mkdir -p "$verification_cache" || die 'cannot create verification cache'
    receipt_tmp=$(mktemp "$verification_cache/.receipt.XXXXXX") || die 'cannot create verification receipt'
    printf 'identity=%s\ndigest=%s\n' "$identity" "$digest" >"$receipt_tmp"
    chmod 0600 "$receipt_tmp"
    mv -f "$receipt_tmp" "$verification_receipt"
  fi
else
  die 'cosign is required to verify the image signature'
fi

pins="$run_tmp/pins"
if [[ "$operation" == sync && "$templates_enabled" == true ]]; then
  : >"$run_tmp/new-lock"
  while read -r template_repository _; do
    oid=$(bounded git ls-remote "https://github.com/${template_repository}.git" HEAD 2>/dev/null \
      | awk 'NR == 1 {print $1}') \
      || die "cannot resolve default branch for $template_repository"
    [[ "$oid" =~ ^[0-9a-f]{40}$ ]] || die "cannot resolve default branch for $template_repository"
    printf '%s %s\n' "$template_repository" "$oid" >>"$run_tmp/new-lock"
  done <"$pins"
  pins="$run_tmp/new-lock"
fi

mounts=()
arguments=()
index=0
cache=$cache_home/workflow-containers/templates
invalid_cache() { die "template cache is invalid; remove $source_dir"; }
while read -r template_repository oid; do
  [[ "$oid" =~ ^[0-9a-f]{40}$ ]] || die "invalid OID for $template_repository"
  owner_name=${template_repository%%/*}
  repository_name=${template_repository#*/}
  source_dir="$cache/$owner_name/$repository_name/$oid"
  if [[ ! -d "$source_dir/.git" ]]; then
    parent=${source_dir%/*}
    mkdir -p "$parent" || die "cannot create cache for $template_repository"
    cache_tmp=$(mktemp -d "$parent/.${oid}.tmp.XXXXXX") || die "cannot create cache for $template_repository"
    git -C "$cache_tmp" init -q || die "cannot initialize cache for $template_repository"
    git -C "$cache_tmp" remote add origin "https://github.com/${template_repository}.git" \
      || die "cannot initialize cache for $template_repository"
    bounded git -C "$cache_tmp" fetch --quiet --depth=1 origin "$oid" \
      || die "cannot fetch $template_repository@$oid"
    git -C "$cache_tmp" checkout --quiet --detach FETCH_HEAD || die "cannot check out $template_repository@$oid"
    [[ $(git -C "$cache_tmp" rev-parse HEAD) == "$oid" ]] || die "checkout mismatch for $template_repository"
    chmod -R a+rX,a-w "$cache_tmp" || die "cannot protect cache for $template_repository"
    mv "$cache_tmp" "$source_dir" || die "cannot install cache for $template_repository"
    cache_tmp=''
  fi
  cache_head=$(git -C "$source_dir" rev-parse HEAD 2>/dev/null) || invalid_cache
  [[ "$cache_head" == "$oid" ]] || invalid_cache
  git -C "$source_dir" cat-file -e "${oid}^{tree}" 2>/dev/null || invalid_cache
  cache_status=$(git -C "$source_dir" --no-optional-locks status \
    --porcelain --ignored --untracked-files=all 2>/dev/null) || invalid_cache
  [[ -z "$cache_status" ]] || invalid_cache
  mounts+=(--volume "$(host_path "$source_dir"):/templates/$index:ro,z")
  arguments+=(--template "$template_repository=/templates/$index")
  index=$((index + 1))
done <"$pins"
if [[ "$templates_enabled" == true && $index -eq 0 ]]; then die 'no external templates remain after self-skip'; fi

user_args=()
runtime_extra=()
if [[ "$runtime" == podman ]]; then
  user_args=(--userns=keep-id --user "$(id -u):$(id -g)")
  runtime_extra=(--read-only-tmpfs=false)
else
  user_args=(--user "$(id -u):$(id -g)")
fi

bounded_runtime image inspect --format '{{json .Config.Labels}}' "$image_ref" \
  >"$run_tmp/labels.json" || die 'cannot inspect image scratch label'
label_status=0
"$python" - "$run_tmp/labels.json" <<'PY' || label_status=$?
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        labels = json.load(source)
    if labels is None:
        label = ""
    elif isinstance(labels, dict):
        label = labels.get("org.nwarila.workflow.scratch", "")
        if not isinstance(label, str):
            raise ValueError
    else:
        raise ValueError
except (TypeError, ValueError, OSError, json.JSONDecodeError):
    raise SystemExit(2)
raise SystemExit(0 if label == "true" else 1)
PY
[[ "$label_status" -le 1 ]] || die 'image scratch labels were not valid JSON'
scratch=()
if [[ "$label_status" -eq 0 ]]; then
  if [[ "$runtime" == podman ]]; then
    scratch=(--tmpfs '/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777,notmpcopyup' \
      --tmpfs '/home/nonroot:rw,nosuid,nodev,noexec,size=16m,mode=1777,notmpcopyup')
  else
    scratch=(--tmpfs '/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777' \
      --tmpfs '/home/nonroot:rw,nosuid,nodev,noexec,size=16m,mode=1777')
  fi
fi

if [[ "$templates_enabled" == true ]]; then
  threshold=(--fail-on "$fail_on")
else
  arguments=(--config "/workspace/$config_rel")
  threshold=()
fi
format=text
[[ "$operation" == sync ]] && format='patch'
run_args=(run --rm --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  ${runtime_extra[@]+"${runtime_extra[@]}"} ${user_args[@]+"${user_args[@]}"} \
  ${scratch[@]+"${scratch[@]}"} --volume "$(host_path "$root"):/workspace:ro,z" \
  ${mounts[@]+"${mounts[@]}"} "$image_ref" --workspace /workspace \
  ${arguments[@]+"${arguments[@]}"} ${threshold[@]+"${threshold[@]}"} --format "$format")

status=0
if bounded_runtime "${run_args[@]}" >"$run_tmp/output" 2>"$run_tmp/error"; then status=0; else status=$?; fi
if [[ ! "$status" =~ ^[012]$ ]]; then
  cat "$run_tmp/error" >&2
  die "unexpected or timed-out container status $status"
fi

if [[ "$operation" == check ]]; then
  if [[ "$status" -eq 0 ]]; then
    many='([2-9]|[1-9][0-9]+)'
    not_one='(0|[2-9]|[1-9][0-9]+)'
    template_count="(1 template|${many} templates)"
    checks="(1 check|${many} checks)"
    errors="(1 error|${not_one} errors)"
    warnings="(1 warning|${not_one} warnings)"
    expected="^${name}: (PASS|WARNING \\([0-9]+ findings\\)|PASS \\(${template_count}, ${checks}, 0 errors, 0 warnings\\)|WARNING \\(${template_count}, ${checks}, ${errors}, ${warnings}\\))$"
    if [[ "$templates_enabled" == true ]]; then
      if ! tail -n 1 "$run_tmp/output" | LC_ALL=C grep -qaxE "$expected"; then status=2; fi
    elif ! tail -n 1 "$run_tmp/output" | LC_ALL=C grep -qaxE "${name}: (PASS|WARNING)( \\([ -'*-~]+\\))?"; then
      status=2
    elif ! tail -c 1 "$run_tmp/output" | LC_ALL=C grep -qax ''; then
      status=2
    fi
  fi
  cat "$run_tmp/output"
  cat "$run_tmp/error" >&2
  launcher_complete=1
  exit "$status"
fi

cat "$run_tmp/error" >&2
[[ "$status" -ne 2 ]] || die 'checker returned a tool error'
patch="$run_tmp/output"
if [[ "$status" -eq 0 && -s "$patch" ]]; then
  die 'checker returned a patch with status 0'
elif [[ "$status" -eq 1 && ! -s "$patch" ]]; then
  die 'checker returned status 1 without a patch'
fi
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
    case "$path" in "$lock_rel" | "$lock_rel"/*) die "patch changes $lock_rel; nothing was changed" ;; esac
    printf '%s\0' "$path" >>"$run_tmp/paths"
    require_path_writable "$path"
  done <"$run_tmp/numstat"
fi
if [[ "$templates_enabled" == true ]]; then
  lock_ancestor=$(nearest_directory "$lock")
  [[ -w "$lock_ancestor" && -x "$lock_ancestor" ]] || die "not writable: $lock_rel; nothing was changed"
  lock_tmp=$(mktemp "${lock%/*}/.workflow-container.lock.XXXXXX") || die 'cannot create lock temporary'
  if ! cp "$pins" "$lock_tmp" || ! chmod 0644 "$lock_tmp"; then
    rm -f "$lock_tmp"; die 'cannot prepare lock temporary'
  fi
fi
if [[ -s "$patch" ]] && ! git apply -- "$patch" 2>"$run_tmp/apply.stderr"; then
  [[ "$templates_enabled" != true ]] || rm -f "$lock_tmp"
  retain_run=1
  die "patch application failed; recovery patch retained at $patch"
fi
if [[ "$templates_enabled" == true ]] \
  && { [[ -d "$lock" || -L "$lock" ]] || ! mv -f "$lock_tmp" "$lock" 2>/dev/null; }; then
  rm -f "$lock_tmp"
  if [[ -s "$patch" ]] && ! git apply -R -- "$patch" >/dev/null 2>&1; then
    retain_run=1
    die "lock write failed; patch revert failed; recovery patch retained at $patch"
  fi
  die 'lock write failed; patch reverted'
fi
printf '%s sync changed:\n' "$name"
git diff --stat -- .
launcher_complete=1
