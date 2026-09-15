#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'usage: tools/template-drift.sh --template <owner/repo> --ref <sha> [--config <path>] [--fail-on <error|warning>]' >&2
}

template=''
template_ref=''
config='template-drift.json'
fail_on='error'

while (($#)); do
  case "$1" in
    --template)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      template=$2
      shift 2
      ;;
    --ref)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      template_ref=$2
      shift 2
      ;;
    --config)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      config=$2
      shift 2
      ;;
    --fail-on)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      fail_on=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ ! "${template}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
   [[ "${template%%/*}" == "." || "${template%%/*}" == ".." ||
      "${template#*/}" == "." || "${template#*/}" == ".." ]]; then
  printf '%s\n' 'error: --template must be an owner/repository name' >&2
  exit 2
fi
if [[ ! "${template_ref}" =~ ^[0-9a-f]{40}$ ]]; then
  printf '%s\n' 'error: --ref must be a 40-character lowercase commit SHA' >&2
  exit 2
fi
if [[ "${fail_on}" != error && "${fail_on}" != warning ]]; then
  printf '%s\n' 'error: --fail-on must be error or warning' >&2
  exit 2
fi
if [[ "${config}" == /* || -z "${config}" ]]; then
  printf '%s\n' 'error: --config must be relative to the template root' >&2
  exit 2
fi

if command -v podman >/dev/null 2>&1; then
  runtime='podman'
elif command -v docker >/dev/null 2>&1; then
  runtime='docker'
else
  printf '%s\n' 'error: podman or docker is required' >&2
  exit 2
fi

owner=${template%%/*}
repository=${template#*/}
cache_root=${XDG_CACHE_HOME:-${HOME}/.cache}/template-drift
cache_parent=${cache_root}/${owner}/${repository}
source_dir=${cache_parent}/${template_ref}
checkout_tmp=''
run_tmp=''

# ShellCheck cannot infer that EXIT invokes this function.
# shellcheck disable=SC2317
cleanup() {
  if [[ -n "${checkout_tmp}" && -d "${checkout_tmp}" ]]; then
    rm -rf -- "${checkout_tmp}"
  fi
  if [[ -n "${run_tmp}" && -d "${run_tmp}" ]]; then
    rm -rf -- "${run_tmp}"
  fi
}
trap cleanup EXIT

if [[ ! -d "${source_dir}/.git" ]]; then
  if [[ -e "${source_dir}" ]]; then
    printf 'error: cache path exists but is not a git checkout: %s\n' "${source_dir}" >&2
    exit 2
  fi
  mkdir -p "${cache_parent}"
  checkout_tmp=$(mktemp -d "${cache_parent}/.${template_ref}.tmp.XXXXXX")
  git init -q "${checkout_tmp}"
  git -C "${checkout_tmp}" remote add origin "https://github.com/${template}.git"
  git -C "${checkout_tmp}" fetch --quiet --depth=1 origin "${template_ref}"
  git -C "${checkout_tmp}" checkout --quiet --detach FETCH_HEAD
  if [[ $(git -C "${checkout_tmp}" rev-parse HEAD) != "${template_ref}" ]]; then
    printf '%s\n' 'error: fetched template commit does not match --ref' >&2
    exit 2
  fi
  chmod -R a+rX,a-w "${checkout_tmp}"
  mv "${checkout_tmp}" "${source_dir}"
  checkout_tmp=''
fi

if [[ $(git -C "${source_dir}" rev-parse HEAD) != "${template_ref}" ]]; then
  printf 'error: cached checkout HEAD does not match --ref: %s\n' "${source_dir}" >&2
  exit 2
fi

# A checkout created under a 007 umask is not readable by image UID 65532.
chmod -R a+rX "${PWD}" "${source_dir}"

# renovate: datasource=docker depName=ghcr.io/nwarila-platform/workflow-template-drift
image='ghcr.io/nwarila-platform/workflow-template-drift:1.0.1@sha256:f969139e479618f59cb57d02053061c82bb5d89815a53d05ac878dffaea7e35d'
"${runtime}" pull --quiet "${image}" >/dev/null

run_tmp=$(mktemp -d "${TMPDIR:-/tmp}/template-drift.XXXXXX")
result=${run_tmp}/result.json
patch_candidate=${run_tmp}/template-drift.patch
status=0
"${runtime}" run --rm --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --volume "${PWD}:/workspace:ro" \
  --volume "${source_dir}:/source:ro" \
  "${image}" \
  --workspace /workspace \
  --source /source \
  --config "${config}" \
  --fail-on "${fail_on}" \
  --format json >"${result}" || status=$?

cat "${result}"
if jq -ej 'select(.version == 1 and (.patch | type == "string")) | .patch' \
  "${result}" >"${patch_candidate}" 2>/dev/null && [[ -s "${patch_candidate}" ]]; then
  install -m 0644 "${patch_candidate}" "${PWD}/template-drift.patch"
fi

exit "${status}"
