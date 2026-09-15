#!/usr/bin/env bash
# Purpose: install the reviewed Crane release after verifying its literal archive SHA-256.

set -euo pipefail

version="${CRANE_VERSION:-v0.21.7}"
dest="${1:-dist/tools}"
tmp_dir="$(mktemp -d)"

cleanup() {
  rm -rf "${tmp_dir}"
}
trap cleanup EXIT

if [[ "${version}" != "v0.21.7" ]]; then
  echo "unsupported Crane version without a reviewed archive sha256: ${version}" >&2
  exit 1
fi

case "$(uname -s)" in
  Linux*) os="Linux" ;;
  *) echo "unsupported OS for Crane install: $(uname -s)" >&2; exit 1 ;;
esac

case "$(uname -m)" in
  x86_64 | amd64)
    arch="x86_64"
    archive_sha256="1a57bc98207fa1c0d04bf760699099e26f8383499bfd55b99c1b919a928a7230"
    ;;
  aarch64 | arm64)
    arch="arm64"
    archive_sha256="b6ee979d9411dfb05ce35ab9e156fe5de7def11a230764a7856ffa2eb971fa88"
    ;;
  *) echo "unsupported architecture for Crane install: $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "${dest}"
archive="go-containerregistry_${os}_${arch}.tar.gz"
archive_path="${tmp_dir}/${archive}"
base_url="https://github.com/google/go-containerregistry/releases/download/${version}"

curl -fsSL -o "${archive_path}" "${base_url}/${archive}"
printf '%s  %s\n' "${archive_sha256}" "${archive_path}" | sha256sum -c -
tar xzf "${archive_path}" -C "${tmp_dir}" crane
install -m 0755 "${tmp_dir}/crane" "${dest}/crane"

installed_version="$("${dest}/crane" version)"
if [[ "${installed_version}" != "${version#v}" ]]; then
  echo "installed Crane version mismatch: expected ${version#v}, got ${installed_version}" >&2
  exit 1
fi
printf 'crane version %s\n' "${installed_version}"
