from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_BIN = ROOT / ".runtime" / "bin"
ARTIFACTS = ROOT / "artifacts"
CLI = ["python3.12", "-I", "-X", "utf8", "-B", "-m", "workflow_template_drift"]


def check(
    mode: str,
    *,
    path: str | None = None,
    source: str | None = None,
    target: str | None = None,
    severity: str | None = None,
    head_lines: object | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {"mode": mode}
    if path is not None:
        item["path"] = path
    if source is not None:
        item["source"] = source
    if target is not None:
        item["target"] = target
    if severity is not None:
        item["severity"] = severity
    if head_lines is not None:
        item["head_lines"] = head_lines
    return item


def config(checks: list[dict[str, object]]) -> dict[str, object]:
    return {"version": 3, "checks": checks}


def runtime_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(RUNTIME_BIN) + os.pathsep + env.get("PATH", "")
    if overrides:
        env.update(overrides)
    return env


def invoke(
    workspace: Path,
    source: Path,
    *,
    fail_on: str = "error",
    config_path: str = "drift.json",
    overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        CLI
        + [
            "--workspace",
            str(workspace),
            "--source",
            str(source),
            "--config",
            config_path,
            "--fail-on",
            fail_on,
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=runtime_env(overrides),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def decode(test: unittest.TestCase, completed: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
    test.assertEqual(completed.stderr, b"")
    test.assertTrue(completed.stdout.endswith(b"\n"))
    test.assertFalse(completed.stdout.endswith(b"\n\n"))
    test.assertLessEqual(len(completed.stdout), 8_388_608)
    result = json.loads(completed.stdout)
    if completed.returncode == 2:
        test.assertEqual(set(result), {"version", "status", "error"})
        test.assertEqual(result["status"], "ERROR")
        test.assertEqual(set(result["error"]), {"code", "message"} | ({"path"} if "path" in result["error"] else set()))
    else:
        test.assertEqual(set(result), {"version", "status", "findings", "patch"})
        test.assertIn(result["status"], {"PASS", "WARNING", "FAIL"})
        for finding in result["findings"]:
            test.assertEqual(
                set(finding),
                {"target", "mode", "severity", "kind", "fixable", "details"},
            )
    test.assertEqual(result["version"], 1)
    return result


class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "tmp").mkdir(exist_ok=True)
        self.temp_context = tempfile.TemporaryDirectory(dir=ARTIFACTS / "tmp")
        self.case = Path(self.temp_context.name)
        self.source = self.case / "source"
        self.workspace = self.case / "workspace"
        self.source.mkdir()
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self.temp_context.cleanup()

    def put(self, root: Path, relative: str, data: bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def configure(self, value: object, *, raw: bool = False) -> None:
        data = value if raw else (json.dumps(value, separators=(",", ":")) + "\n").encode()
        assert isinstance(data, bytes)
        self.put(self.source, "drift.json", data)

    def invoke(
        self,
        fail_on: str = "error",
        config_path: str = "drift.json",
        overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return invoke(
            self.workspace,
            self.source,
            fail_on=fail_on,
            config_path=config_path,
            overrides=overrides,
        )

    def result(self, completed: subprocess.CompletedProcess[bytes] | None = None) -> dict[str, object]:
        return decode(self, self.invoke() if completed is None else completed)
