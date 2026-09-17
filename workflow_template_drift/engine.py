"""Core evaluator. The public ``evaluate`` function has no write side effects."""
from __future__ import annotations
import difflib
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any
MAX_OUTPUT = 8_388_608
MAX_CONFIG_BYTES = 1_048_576
MAX_JSON_DEPTH = 32
MAX_CHECKS = 4_096
MAX_FILE_BYTES = 67_108_864
MAX_TOTAL_BYTES = 536_870_912
MAX_HEAD_LINES = 10_000
PATH_RE = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z", re.ASCII)
MODES = {"bytes_equal", "must_exist", "head_lines_equal", "must_be_absent"}
SEVERITIES = {"error", "warning"}
KINDS = {
    "target_missing",
    "target_not_regular",
    "content_mismatch",
    "target_malformed",
    "target_too_short",
    "target_present",
}
_O_BASE = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


class EvaluationError(Exception):
    """A bounded, user-visible exit-2 condition."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


@dataclass(frozen=True)
class Check:
    template: str
    severity: str
    mode: str
    target: str
    source: str | None = None
    head_lines: int | None = None


@dataclass
class SourceObject:
    fd: int
    before: os.stat_result
    data: bytes | None = None


@dataclass(frozen=True)
class TargetObject:
    shape: str
    fd: int | None = None
    before: os.stat_result | None = None


@dataclass
class ReadBudget:
    total: int = 0

    def reserve(self, size: int, path: str) -> None:
        if self.total + size > MAX_TOTAL_BYTES:
            raise EvaluationError(
                "resource_limit",
                "total bytes read would exceed 536870912-byte limit",
                path=path,
            )
        self.total += size


def _path_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.encode("utf-8", "strict")) <= 1024
        and value.isascii()
        and PATH_RE.fullmatch(value) is not None
        and all(part not in {".", ".."} for part in value.split("/"))
    )


def _require_path(value: Any, field: str) -> str:
    try:
        valid = _path_ok(value)
    except UnicodeError:
        valid = False
    if not valid:
        raise EvaluationError("invalid_config", f"{field} is not a valid repo-relative POSIX path")
    return value


def _git_apply_target_ok(value: str) -> bool:
    return all(part.lower().rstrip(".") != ".git" for part in value.split("/"))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-JSON constant {value}")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object member")
        result[key] = value
    return result


def _validate_json_strings(value: Any) -> None:
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("NUL in JSON string")
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("unpaired surrogate code point")
    elif isinstance(value, list):
        for item in value:
            _validate_json_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_json_strings(key)
            _validate_json_strings(item)


def _check_json_depth(raw: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise EvaluationError("resource_limit", "JSON nesting depth exceeds 32-level limit")
        elif byte in (0x5D, 0x7D):
            depth -= 1


def _parse_config(raw: bytes) -> Any:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise EvaluationError("invalid_config", "config must not start with a UTF-8 BOM")
    if b"\x00" in raw:
        raise EvaluationError("invalid_config", "config must not contain NUL")
    _check_json_depth(raw)
    try:
        text = raw.decode("utf-8", "strict")
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
        _validate_json_strings(value)
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise EvaluationError("invalid_config", f"config is not strict JSON: {exc}") from None


def _exact_keys(obj: Any, required: set[str], optional: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise EvaluationError("invalid_config", f"{where} must be an object")
    actual = set(obj)
    missing = required - actual
    if missing:
        raise EvaluationError("invalid_config", f"{where} is missing key {sorted(missing)[0]!r}")
    if actual - required - optional:
        raise EvaluationError("invalid_config", f"{where} has an unknown key")
    return obj


def _schema_config(value: Any, template: str) -> list[Check]:
    root = _exact_keys(value, {"version", "checks"}, set(), "config")
    version = root["version"]
    if not (isinstance(version, int) and not isinstance(version, bool) and version == 3):
        raise EvaluationError("invalid_config", "config.version must be the integer 3")
    items = root["checks"]
    if not isinstance(items, list):
        raise EvaluationError("invalid_config", "config.checks must be an array")
    if not items:
        raise EvaluationError("invalid_config", "config.checks must contain at least one check")
    if len(items) > MAX_CHECKS:
        raise EvaluationError("resource_limit", "config.checks exceeds 4096-check limit")
    checks: list[Check] = []
    for index, item in enumerate(items):
        where = f"config.checks[{index}]"
        if not isinstance(item, dict):
            raise EvaluationError("invalid_config", f"{where} must be an object")
        mode = item.get("mode")
        if not isinstance(mode, str) or mode not in MODES:
            raise EvaluationError("invalid_config", f"{where}.mode is unknown")
        has_path = "path" in item
        has_pair = "source" in item or "target" in item
        if has_path == has_pair or (has_pair and not {"source", "target"}.issubset(item)):
            raise EvaluationError(
                "invalid_config",
                f"{where} must have either path or both source and target",
            )
        if mode == "must_be_absent" and not has_path:
            raise EvaluationError("invalid_config", f"{where} must_be_absent takes path only")
        required = {"mode", "path"} if has_path else {"mode", "source", "target"}
        if mode == "head_lines_equal":
            required.add("head_lines")
        obj = _exact_keys(item, required, {"severity"}, where)
        severity = obj.get("severity", "error")
        if not isinstance(severity, str) or severity not in SEVERITIES:
            raise EvaluationError("invalid_config", f"{where}.severity must be error or warning")
        if has_path:
            path = _require_path(obj["path"], f"{where}.path")
            target = path
            source = None if mode in {"must_exist", "must_be_absent"} else path
        else:
            source = _require_path(obj["source"], f"{where}.source")
            target = _require_path(obj["target"], f"{where}.target")
        count = obj.get("head_lines")
        if mode == "head_lines_equal":
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise EvaluationError(
                    "invalid_config",
                    f"{where}.head_lines must be an integer from 1 through 10000",
                )
            if count > MAX_HEAD_LINES:
                raise EvaluationError(
                    "resource_limit",
                    f"{where}.head_lines exceeds 10000-line limit",
                )
        if not _git_apply_target_ok(target):
            raise EvaluationError(
                "invalid_config",
                f"{where} target is not safe for git apply",
            )
        checks.append(Check(template, severity, mode, target, source, count))
    return checks


def _validate_targets(checks: list[Check]) -> None:
    seen: list[Check] = []
    if len(checks) > MAX_CHECKS:
        raise EvaluationError("resource_limit", "combined configs exceed 4096-check limit")
    for check in checks:
        for other in seen:
            if (check.target == other.target or check.target.startswith(other.target + "/")
                    or other.target.startswith(check.target + "/")):
                raise EvaluationError(
                    "invalid_config",
                    f"target {check.target!r} from {check.template} overlaps "
                    f"target {other.target!r} from {other.template}",
                )
        seen.append(check)


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _walk_parent(root_fd: int, path: str, *, target: bool) -> tuple[int, str] | None:
    parts = path.split("/")
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            try:
                following = os.open(component, _O_BASE | _O_DIRECTORY | _O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                if target:
                    os.close(current)
                    return None
                raise EvaluationError("source_missing", "source path does not exist", path=path) from None
            except OSError:
                raise EvaluationError(
                    "io_error",
                    "unsafe or inaccessible intermediate component",
                    path=path,
                ) from None
            os.close(current)
            current = following
        return current, parts[-1]
    except Exception:
        try:
            os.close(current)
        except OSError:
            pass
        raise


def _lstat_final(root_fd: int, path: str, *, target: bool) -> tuple[int, str, os.stat_result] | None:
    parent = _walk_parent(root_fd, path, target=target)
    if parent is None:
        return None
    parent_fd, name = parent
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.close(parent_fd)
        return None
    except OSError:
        os.close(parent_fd)
        raise EvaluationError("io_error", "cannot inspect path", path=path) from None
    return parent_fd, name, before


def _open_regular(root_fd: int, path: str, *, config: bool = False) -> SourceObject:
    entry = _lstat_final(root_fd, path, target=False)
    if entry is None:
        code = "config_missing" if config else "source_missing"
        label = "config" if config else "source"
        raise EvaluationError(code, f"{label} path does not exist", path=path)
    parent_fd, name, before = entry
    label = "config" if config else "source"
    try:
        if not stat.S_ISREG(before.st_mode):
            code = "invalid_config" if config else "invalid_source"
            raise EvaluationError(code, f"{label} must be a regular non-symlink file", path=path)
        try:
            fd = os.open(name, _O_BASE | _O_NOFOLLOW, dir_fd=parent_fd)
        except OSError:
            raise EvaluationError("io_error", f"cannot open {label}", path=path) from None
        opened = os.fstat(fd)
        if not _same_snapshot(before, opened):
            os.close(fd)
            raise EvaluationError("io_error", f"{label} changed while being opened", path=path)
        limit = MAX_CONFIG_BYTES if config else MAX_FILE_BYTES
        if opened.st_size > limit:
            os.close(fd)
            message = (
                "config exceeds 1048576-byte limit"
                if config
                else "source exceeds 67108864-byte file limit"
            )
            raise EvaluationError("resource_limit", message, path=path)
        return SourceObject(fd, opened)
    finally:
        os.close(parent_fd)


def _read_stable(obj: SourceObject, path: str, label: str, budget: ReadBudget) -> bytes:
    budget.reserve(obj.before.st_size, path)
    try:
        os.lseek(obj.fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = obj.before.st_size
        while remaining:
            chunk = os.read(obj.fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(obj.fd)
    except OSError:
        raise EvaluationError("io_error", f"cannot read {label}", path=path) from None
    if not _same_snapshot(obj.before, after) or remaining:
        raise EvaluationError("io_error", f"{label} changed while being read", path=path)
    return b"".join(chunks)


def _target_object(root_fd: int, path: str) -> TargetObject:
    entry = _lstat_final(root_fd, path, target=True)
    if entry is None:
        return TargetObject("missing")
    parent_fd, name, before = entry
    try:
        if stat.S_ISLNK(before.st_mode):
            return TargetObject("symlink")
        if stat.S_ISDIR(before.st_mode):
            return TargetObject("directory")
        if not stat.S_ISREG(before.st_mode):
            return TargetObject("special")
        try:
            fd = os.open(name, _O_BASE | _O_NOFOLLOW, dir_fd=parent_fd)
        except OSError:
            raise EvaluationError("io_error", "cannot open target", path=path) from None
        opened = os.fstat(fd)
        if not _same_snapshot(before, opened):
            os.close(fd)
            raise EvaluationError("io_error", "target changed while being opened", path=path)
        if opened.st_size > MAX_FILE_BYTES:
            os.close(fd)
            raise EvaluationError(
                "resource_limit",
                "target exceeds 67108864-byte file limit",
                path=path,
            )
        return TargetObject("regular", fd, opened)
    finally:
        os.close(parent_fd)


def _read_target(obj: TargetObject, path: str, budget: ReadBudget) -> bytes:
    assert obj.fd is not None and obj.before is not None
    return _read_stable(SourceObject(obj.fd, obj.before), path, "target", budget)


def _text(data: bytes) -> str | None:
    if data.startswith(b"\xef\xbb\xbf") or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None


def _lines(data: bytes) -> list[bytes]:
    result: list[bytes] = []
    start = 0
    while True:
        index = data.find(b"\n", start)
        if index < 0:
            if start < len(data):
                result.append(data[start:])
            return result
        result.append(data[start:index + 1])
        start = index + 1


def _patch_lines(
    old: bytes | None,
    new: bytes | None,
    target: str,
    old_mode: int | None = None,
) -> str:
    if old is None and new == b"":
        return (
            f"diff --git a/{target} b/{target}\n"
            "new file mode 100644\n"
            "index 0000000..e69de29\n"
        )
    if old == b"" and new is None:
        git_mode = "100755" if old_mode is not None and old_mode & 0o111 else "100644"
        return (
            f"diff --git a/{target} b/{target}\n"
            f"deleted file mode {git_mode}\n"
            "index e69de29..0000000\n"
        )
    old_lines = [line.decode("utf-8") for line in _lines(b"" if old is None else old)]
    new_lines = [line.decode("utf-8") for line in _lines(b"" if new is None else new)]
    fromfile = "/dev/null" if old is None else f"a/{target}"
    tofile = "/dev/null" if new is None else f"b/{target}"
    raw = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=fromfile,
        tofile=tofile,
        n=3,
        lineterm="\n",
    )
    output: list[str] = [f"diff --git a/{target} b/{target}\n"]
    if old is None:
        output.append("new file mode 100644\n")
    elif new is None:
        git_mode = "100755" if old_mode is not None and old_mode & 0o111 else "100644"
        output.append(f"deleted file mode {git_mode}\n")
    for line in raw:
        if line.endswith("\n"):
            output.append(line)
        else:
            output.append(line + "\n")
            if line and line[0] in " +-":
                output.append("\\ No newline at end of file\n")
    return "".join(output)


def _finding(check: Check, kind: str, fixable: bool) -> dict[str, Any]:
    assert kind in KINDS
    return {
        "target": check.target,
        "mode": check.mode,
        "severity": check.severity,
        "kind": kind,
        "template": check.template,
        "fixable": fixable,
    }


def _preflight(
    source_fd: int,
    checks: list[Check],
    budget: ReadBudget,
) -> dict[str, SourceObject]:
    sources: dict[str, SourceObject] = {}
    try:
        for check in checks:
            if check.source is None or check.source in sources:
                continue
            sources[check.source] = _open_regular(source_fd, check.source)
        for check in checks:
            if check.source is None:
                continue
            obj = sources[check.source]
            if check.mode in {"bytes_equal", "head_lines_equal"} and obj.data is None:
                obj.data = _read_stable(obj, check.source, "source", budget)
            if check.mode == "head_lines_equal":
                assert obj.data is not None and check.head_lines is not None
                if _text(obj.data) is None:
                    raise EvaluationError(
                        "invalid_source",
                        "head_lines_equal source is not strict UTF-8 text without BOM or NUL",
                        path=check.source,
                    )
                if len(_lines(obj.data)) < check.head_lines:
                    raise EvaluationError(
                        "invalid_source",
                        "head_lines_equal source has fewer than head_lines lines",
                        path=check.source,
                    )
        return sources
    except Exception:
        for obj in sources.values():
            try:
                os.close(obj.fd)
            except OSError:
                pass
        raise


def _evaluate_check(
    check: Check,
    workspace_fd: int,
    source: SourceObject | None,
    budget: ReadBudget,
) -> tuple[dict[str, str] | None, str]:
    target_obj = _target_object(workspace_fd, check.target)
    patch = ""
    try:
        if check.mode == "must_be_absent":
            if target_obj.shape == "missing":
                return None, patch
            fixable = False
            if target_obj.shape == "regular":
                actual = _read_target(target_obj, check.target, budget)
                if _text(actual) is not None:
                    fixable = True
                    assert target_obj.before is not None
                    patch = _patch_lines(actual, None, check.target, target_obj.before.st_mode)
            return _finding(check, "target_present", fixable), patch

        if check.mode == "must_exist":
            if target_obj.shape == "regular":
                return None, patch
            fixable = False
            if target_obj.shape == "missing" and source is not None:
                if source.data is None:
                    assert check.source is not None
                    source.data = _read_stable(source, check.source, "source", budget)
                if _text(source.data) is not None:
                    fixable = True
                    patch = _patch_lines(None, source.data, check.target)
            kind = "target_missing" if target_obj.shape == "missing" else "target_not_regular"
            return _finding(check, kind, fixable), patch

        assert source is not None and source.data is not None
        desired = source.data
        if target_obj.shape != "regular":
            fixable = target_obj.shape == "missing" and _text(desired) is not None
            if fixable:
                if check.mode == "head_lines_equal":
                    assert check.head_lines is not None
                    desired_for_patch = b"".join(_lines(desired)[:check.head_lines])
                else:
                    desired_for_patch = desired
                patch = _patch_lines(None, desired_for_patch, check.target)
            kind = "target_missing" if target_obj.shape == "missing" else "target_not_regular"
            return _finding(check, kind, fixable), patch

        actual = _read_target(target_obj, check.target, budget)
        if check.mode == "bytes_equal":
            if desired == actual:
                return None, patch
            fixable = _text(desired) is not None and _text(actual) is not None
            if fixable:
                patch = _patch_lines(actual, desired, check.target)
            return _finding(check, "content_mismatch", fixable), patch

        assert check.mode == "head_lines_equal" and check.head_lines is not None
        if _text(actual) is None:
            return _finding(check, "target_malformed", False), patch
        source_lines = _lines(desired)
        target_lines = _lines(actual)
        count = check.head_lines
        if len(target_lines) < count:
            replacement = b"".join(source_lines[:count])
            patch = _patch_lines(actual, replacement, check.target)
            return _finding(check, "target_too_short", True), patch
        if source_lines[:count] == target_lines[:count]:
            return None, patch
        desired_target = b"".join(source_lines[:count] + target_lines[count:])
        patch = _patch_lines(actual, desired_target, check.target)
        return _finding(check, "content_mismatch", True), patch
    finally:
        if target_obj.fd is not None:
            os.close(target_obj.fd)


def _error_result(error: EvaluationError) -> dict[str, Any]:
    item: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.path is not None:
        item["path"] = error.path
    return {"status": "ERROR", "error": item}


MESSAGES: dict[str, str] = {
    "target_missing": "target is missing",
    "target_not_regular": "target is not a regular file",
    "content_mismatch": "target content differs from the pinned template",
    "target_malformed": "target is not strict UTF-8 text without BOM or NUL",
    "target_too_short": "target has fewer than the required head lines",
    "target_present": "target must be absent",
}
def text_bytes(result: dict[str, Any]) -> bytes:
    """Render the fixed problem-matcher grammar."""
    findings = result["findings"]
    lines = [
        f"{item['severity']}: {item['target']}: {item['mode']}/{item['kind']} "
        f"(template {item['template']}): {MESSAGES[item['kind']]}\n"
        for item in findings
    ]
    if result["status"] == "PASS":
        lines.append("template-drift: PASS\n")
    elif result["status"] == "WARNING":
        lines.append(f"template-drift: WARNING ({len(findings)} findings)\n")
    else:
        fixable = sum(item["fixable"] for item in findings)
        lines.append(f"template-drift: FAIL ({len(findings)} findings, {fixable} fixable)\n")
    return "".join(lines).encode("utf-8")
def patch_bytes(result: dict[str, Any]) -> bytes:
    """Render the aggregate git patch."""
    return result["patch"].encode("utf-8")


def error_bytes(result: dict[str, Any]) -> bytes:
    error = result["error"]
    suffix = f" ({error['path']})" if "path" in error else ""
    return f"template-drift: error: {error['code']}: {error['message']}{suffix}\n".encode()


def evaluate(
    workspace: str,
    template_roots: list[tuple[str, str]],
    config_path: str,
    fail_on: str,
) -> tuple[dict[str, Any], int]:
    """Evaluate one workspace against every template and return a single result."""
    if fail_on not in {"error", "warning"}:
        return _error_result(EvaluationError("invalid_usage", "--fail-on must be error or warning")), 2
    try:
        config_path = _require_path(config_path, "--config")
    except EvaluationError as error:
        return _error_result(error), 2
    source_fds: list[int] = []
    workspace_fd = None
    source_sets: list[dict[str, SourceObject]] = []
    budget = ReadBudget()
    try:
        try:
            workspace_fd = os.open(workspace, _O_BASE | _O_DIRECTORY | _O_NOFOLLOW)
            source_fds = [os.open(root, _O_BASE | _O_DIRECTORY | _O_NOFOLLOW) for _, root in template_roots]
        except OSError:
            raise EvaluationError("io_error", "cannot open mount root") from None
        grouped: list[list[Check]] = []
        for (name, _), source_fd in zip(template_roots, source_fds, strict=True):
            config_obj = _open_regular(source_fd, config_path, config=True)
            try:
                raw = _read_stable(config_obj, config_path, "config", budget)
            finally:
                os.close(config_obj.fd)
            grouped.append(_schema_config(_parse_config(raw), name))
        checks = [check for group in grouped for check in group]
        _validate_targets(checks)
        source_sets = [_preflight(fd, group, budget) for fd, group in zip(source_fds, grouped, strict=True)]
        evaluated: list[tuple[dict[str, Any], str]] = []
        for group, sources in zip(grouped, source_sets, strict=True):
            for check in group:
                finding, patch = _evaluate_check(check, workspace_fd, sources.get(check.source or ""), budget)
                if finding is not None:
                    evaluated.append((finding, patch))
        evaluated.sort(
            key=lambda pair: tuple(
                part.encode("utf-8") for part in (pair[0]["target"], pair[0]["mode"], pair[0]["template"])
            )
        )
        findings = [pair[0] for pair in evaluated]
        patch = "".join(pair[1] for pair in evaluated)
        error_count = sum(item["severity"] == "error" for item in findings)
        blocked = error_count > 0 if fail_on == "error" else bool(findings)
        status = "FAIL" if blocked else ("WARNING" if findings else "PASS")
        result = {"status": status, "findings": findings, "patch": patch}
        if len(patch.encode("utf-8")) > MAX_OUTPUT or len(text_bytes(result)) > MAX_OUTPUT:
            raise EvaluationError(
                "resource_limit",
                "result exceeds 8388608-byte output limit",
            )
        return result, 1 if blocked else 0
    except EvaluationError as error:
        return _error_result(error), 2
    finally:
        for sources in source_sets:
            for obj in sources.values():
                try:
                    os.close(obj.fd)
                except OSError:
                    pass
        if workspace_fd is not None:
            os.close(workspace_fd)
        for source_fd in source_fds:
            os.close(source_fd)
