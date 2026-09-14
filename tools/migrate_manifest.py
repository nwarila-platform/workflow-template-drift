#!/usr/bin/env python3.12
"""Convert drift-gate v1/v2 manifests to v3 candidates without writing files."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any


PATH_RE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z", re.ASCII)
REVIEW = "review required (must_exist | bytes_equal | omit | conflict)"
MAX_CHECKS = 4_096
MAX_CONFIG_BYTES = 1_048_576


class ManifestError(ValueError):
    pass


def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ManifestError("duplicate object member")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise ManifestError(f"non-JSON constant {value}")


def exact(obj: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict) or set(obj) != keys:
        raise ManifestError(f"{where} has invalid shape")
    return obj


def mapping(value: Any, where: str) -> tuple[str, str]:
    obj = exact(value, {"source", "target"}, where)
    result: list[str] = []
    for field in ("source", "target"):
        item = obj[field]
        if (
            not isinstance(item, str)
            or not item.isascii()
            or not 0 < len(item.encode("utf-8")) <= 1024
            or PATH_RE.fullmatch(item) is None
            or any(part in {".", ".."} for part in item.split("/"))
        ):
            raise ManifestError(f"{where}.{field} is not a valid repo-relative POSIX path")
        result.append(item)
    return result[0], result[1]


def converted_item(source: str, target: str) -> dict[str, str]:
    if source == target:
        return {"mode": "bytes_equal", "path": target, "severity": "error"}
    return {
        "mode": "bytes_equal",
        "source": source,
        "target": target,
        "severity": "error",
    }


def git_apply_target_ok(value: str) -> bool:
    return all(part.lower().rstrip(".") != ".git" for part in value.split("/"))


def convert(value: Any) -> tuple[dict[str, Any], list[str], str]:
    if not isinstance(value, dict):
        raise ManifestError("manifest root must be an object")
    version = value.get("version")
    review: list[str] = []
    mappings: list[tuple[str, str]] = []
    if version == "1":
        root = exact(value, {"version", "files"}, "manifest")
        if not isinstance(root["files"], list):
            raise ManifestError("manifest.files must be an array")
        if len(root["files"]) > MAX_CHECKS:
            raise ManifestError("conversion would exceed v3 4096-check limit")
        mappings = [mapping(item, f"manifest.files[{index}]") for index, item in enumerate(root["files"])]
        summary = f"converted: {len(mappings)} v1 files -> bytes_equal"
    elif version == "2":
        root = exact(value, {"version", "byte_identical", "scaffold_starter"}, "manifest")
        if not isinstance(root["byte_identical"], list) or not isinstance(root["scaffold_starter"], list):
            raise ManifestError("manifest v2 mappings must be arrays")
        if len(root["byte_identical"]) > MAX_CHECKS:
            raise ManifestError("conversion would exceed v3 4096-check limit")
        mappings = [
            mapping(item, f"manifest.byte_identical[{index}]")
            for index, item in enumerate(root["byte_identical"])
        ]
        scaffolds = [
            mapping(item, f"manifest.scaffold_starter[{index}]")
            for index, item in enumerate(root["scaffold_starter"])
        ]
        review = [target for _source, target in scaffolds]
        summary = f"converted: {len(mappings)} v2 byte_identical -> bytes_equal"
    else:
        raise ManifestError('manifest.version must be string "1" or "2"')
    targets: set[str] = set()
    checks: list[dict[str, str]] = []
    for source, target in mappings:
        if not git_apply_target_ok(target):
            raise ManifestError(f"target {target!r} is not safe for git apply")
        if target in targets:
            raise ManifestError(f"duplicate convertible target {target!r}")
        if any(
            target.startswith(other + "/") or other.startswith(target + "/")
            for other in targets
        ):
            raise ManifestError(f"convertible target {target!r} overlaps another target path")
        targets.add(target)
        checks.append(converted_item(source, target))
    if not checks:
        raise ManifestError("conversion would produce an invalid empty v3 checks array")
    return {"version": 3, "checks": checks}, review, summary


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: migrate_manifest.py MANIFEST\n")
        return 2
    try:
        raw = Path(args[0]).read_bytes()
        if raw.startswith(b"\xef\xbb\xbf") or b"\x00" in raw:
            raise ManifestError("manifest is not strict UTF-8 JSON")
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
        document, review, summary = convert(value)
        rendered = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ) + "\n"
        if len(rendered.encode("ascii")) > MAX_CONFIG_BYTES:
            raise ManifestError("conversion would exceed v3 1048576-byte config limit")
    except (OSError, UnicodeError, json.JSONDecodeError, ManifestError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(rendered)
    sys.stderr.write(summary + "\n")
    for path in review:
        sys.stderr.write(f"{path}: {REVIEW}\n")
    sys.stderr.write(f"review required: {len(review)} scaffold_starter paths\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
