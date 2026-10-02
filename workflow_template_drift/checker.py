"""Compare a repository with the templates it follows and report every difference.

A template repository keeps a manifest, ``template-drift.json``, at its root. The manifest is a list
of checks. Each check names one path and the rule that path must satisfy:

    bytes_equal        the file is byte-for-byte identical to the template's copy
    head_lines_equal   the file's first N lines are identical to the template's first N lines
    must_exist         a file exists at the path
    must_be_absent     nothing exists at the path

This module only reads. It builds a report and an exit status, and never changes the repository.
"""

import io
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

MANIFEST = "template-drift.json"
MANIFEST_VERSION = 3
SEVERITIES = ("error", "warning")

# The keys each mode accepts, besides "mode" and the optional "severity". "path" names the same
# path in the template and in the repository; "source" and "target" name the two separately.
SHAPES = {
    "bytes_equal": [{"path"}, {"source", "target"}],
    "head_lines_equal": [{"path", "head_lines"}, {"source", "target", "head_lines"}],
    "must_exist": [{"path"}],
    "must_be_absent": [{"path"}],
}

# Everything a check can find wrong, and how the report words it.
PROBLEMS = {
    "target_missing": "target is missing",
    "target_not_regular": "target is not a regular file",
    "content_mismatch": "target content differs from the pinned template",
    "target_present": "target must be absent",
}

# One segment of a path. Paths are relative and "/"-separated, and "." and ".." are refused, so a
# manifest can only name files inside the template and inside the repository. The narrow character
# set also guarantees that a path can never break the one-finding-per-line report.
SEGMENT = re.compile(r"[A-Za-z0-9._-]+")


class DriftError(Exception):
    """The check could not be carried out, for example because a manifest is malformed."""


@dataclass(frozen=True)
class Check:
    """One rule, as one template declares it."""

    template: str  # the template's label, such as "owner/repo"
    mode: str  # a key of SHAPES
    severity: str  # one of SEVERITIES
    target: str  # the path in the repository being checked
    expected: bytes | None = None  # the template's file; only the two comparing modes have one
    head_lines: int | None = None  # how many leading lines to compare; head_lines_equal only


def check_repository(
    workspace: Path, templates: list[tuple[str, Path]], fail_on: str
) -> tuple[str, int]:
    """Check ``workspace`` against every ``(label, directory)`` template.

    Returns the report and the exit status: 0 when the repository passes, 1 when it does not.
    Raises DriftError when the check cannot be carried out.
    """
    if not workspace.is_dir():
        raise DriftError(f"workspace {workspace} is not a directory")
    checks = []
    for label, directory in templates:
        checks.extend(load_checks(label, directory))

    # Every path has exactly one rule, because two rules for one path could contradict each other.
    owners = {}
    for check in checks:
        if check.target in owners:
            raise DriftError(
                f"{check.target} has two rules: "
                f"one from {owners[check.target]}, one from {check.template}"
            )
        owners[check.target] = check.template

    # One line per broken rule, in path order, so the same repository always gives the same report.
    lines = []
    errors = 0
    warnings = 0
    for check in sorted(checks, key=lambda item: item.target):
        problem = find_problem(check, workspace)
        if problem is None:
            continue
        lines.append(
            f"{check.severity}: {check.target}: {check.mode}/{problem} "
            f"(template {check.template}): {PROBLEMS[problem]}"
        )
        if check.severity == "error":
            errors += 1
        else:
            warnings += 1

    failed = errors > 0 or (warnings > 0 and fail_on == "warning")
    if failed:
        verdict = "FAIL"
    elif warnings > 0:
        verdict = "WARNING"
    else:
        verdict = "PASS"
    lines.append(
        f"template-drift: {verdict} ({_count(len(templates), 'template')}, "
        f"{_count(len(checks), 'check')}, {_count(errors, 'error')}, {_count(warnings, 'warning')})"
    )
    return "".join(line + "\n" for line in lines), 1 if failed else 0


def load_checks(template: str, directory: Path) -> list[Check]:
    """Read one template's manifest and return its checks, refusing anything malformed."""
    if _classify(directory, MANIFEST) != "file":
        raise DriftError(f"{template}: there is no {MANIFEST} file")
    try:
        manifest = json.loads(
            (directory / MANIFEST).read_bytes(), object_pairs_hook=_reject_duplicate_keys
        )
    except ValueError as error:
        raise DriftError(f"{template}: {MANIFEST} is not valid JSON: {error}") from None
    if not isinstance(manifest, dict) or set(manifest) != {"version", "checks"}:
        raise DriftError(f"{template}: {MANIFEST} must hold exactly the keys version and checks")
    version = manifest["version"]
    if type(version) is not int or version != MANIFEST_VERSION:
        raise DriftError(f"{template}: {MANIFEST} version must be {MANIFEST_VERSION}")
    items = manifest["checks"]
    if not isinstance(items, list) or not items:
        raise DriftError(f"{template}: {MANIFEST} checks must be a non-empty list")
    return [
        _load_check(template, directory, item, f"{template}: checks[{index}]")
        for index, item in enumerate(items)
    ]


def find_problem(check: Check, workspace: Path) -> str | None:
    """Return the key of PROBLEMS that says how the repository breaks ``check``, or None."""
    found = _classify(workspace, check.target)
    if check.mode == "must_be_absent":
        return None if found == "missing" else "target_present"
    if found == "missing":
        return "target_missing"
    if found != "file":
        return "target_not_regular"
    if check.mode == "must_exist":
        return None

    actual = (workspace / check.target).read_bytes()
    expected = check.expected
    if check.mode == "head_lines_equal":
        actual = _lines(actual)[: check.head_lines]
        expected = _lines(expected)[: check.head_lines]
    return None if actual == expected else "content_mismatch"


def _load_check(template: str, directory: Path, item: object, where: str) -> Check:
    """Validate one entry of a manifest's ``checks`` list and read the template file it compares."""
    if not isinstance(item, dict):
        raise DriftError(f"{where} must be an object")
    mode = item.get("mode")
    if not isinstance(mode, str) or mode not in SHAPES:
        raise DriftError(f"{where}: mode must be one of {', '.join(SHAPES)}")
    severity = item.get("severity", "error")
    if severity not in SEVERITIES:
        raise DriftError(f"{where}: severity must be error or warning")

    # Unknown keys are refused rather than ignored, so a misspelt key can never weaken a rule.
    shape = set(item) - {"mode", "severity"}
    if shape not in SHAPES[mode]:
        accepted = " or ".join(" + ".join(sorted(keys)) for keys in SHAPES[mode])
        raise DriftError(f"{where}: {mode} takes {accepted}")
    for key in ("path", "source", "target"):
        if key in item and not _is_safe_path(item[key]):
            raise DriftError(f"{where}: {key} must be a relative path such as docs/guide.md")
    source = item.get("source", item.get("path"))
    target = item.get("target", item.get("path"))
    if mode in ("must_exist", "must_be_absent"):
        return Check(template, mode, severity, target)

    # The two comparing modes need the template's file, so a manifest that names a file its own
    # template does not have is refused here, whatever the repository being checked looks like.
    if _classify(directory, source) != "file":
        raise DriftError(f"{where}: the template has no file {source}")
    expected = (directory / source).read_bytes()
    head_lines = item.get("head_lines")
    if mode == "head_lines_equal":
        if type(head_lines) is not int or head_lines < 1:
            raise DriftError(f"{where}: head_lines must be a whole number, 1 or more")
        if len(_lines(expected)) < head_lines:
            raise DriftError(f"{where}: {source} has fewer than {head_lines} lines")
    return Check(template, mode, severity, target, expected, head_lines)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object, refusing a repeated key. Python would silently keep only the last."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"the key {key!r} appears twice in one object")
        result[key] = value
    return result


def _is_safe_path(value: object) -> bool:
    """Accept ``docs/guide.md``; refuse ``../guide.md``, ``/guide.md`` and anything not a string."""
    return isinstance(value, str) and all(
        SEGMENT.fullmatch(segment) and segment not in (".", "..") for segment in value.split("/")
    )


def _classify(root: Path, path: str) -> str:
    """Say what is at ``path`` beneath ``root``: "file", "missing" or "other".

    No part of ``path`` is followed through a symbolic link, so a repository cannot satisfy a rule
    by pointing at a file kept somewhere else. A link at the path itself is "other". A link above
    the path stops the check, because whatever lies beyond it is not part of the tree being checked.
    """
    *parents, name = path.split("/")
    current = root
    for parent in parents:
        current = current / parent
        kind = _kind(current)
        if kind is not None and stat.S_ISLNK(kind):
            raise DriftError(f"{path} lies beneath a symbolic link, and links are never followed")
        if kind is None or not stat.S_ISDIR(kind):
            return "missing"
    kind = _kind(current / name)
    if kind is None:
        return "missing"
    return "file" if stat.S_ISREG(kind) else "other"


def _kind(path: Path) -> int | None:
    """Return the file-type bits of ``path`` itself, never of a link's target; None if absent."""
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None


def _lines(data: bytes) -> list[bytes]:
    """Split ``data`` into lines at each LF, the way ``head -n`` counts; every line keeps its LF."""
    return io.BytesIO(data).readlines()


def _count(number: int, noun: str) -> str:
    """Pair a number with its noun: "1 check", "2 checks"."""
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
