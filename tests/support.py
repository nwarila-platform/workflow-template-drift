from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ["python3.12", "-I", "-X", "utf8", "-B", "-m", "workflow_template_drift"]
FINDING = re.compile(r"^(error|warning): ([^:]+?): ([a-z_]+)/([a-z_]+) \(template ([^)]+)\): (.*)$")
SUMMARY = re.compile(r"^template-drift: (PASS|WARNING|FAIL)(?: \((\d+) findings(?:, (\d+) fixable)?\))?$")
ERROR = re.compile(r"^template-drift: error: ([a-z_]+): (.*?)(?: \(([^()]*)\))?$")

def check(mode: str, **fields: object) -> dict[str, object]:
    return {"mode": mode, **fields}

def config(checks: list[dict[str, object]]) -> dict[str, object]:
    return {"version": 3, "checks": checks}

def invoke(workspace: Path, templates: Path | list[Path], *, config_path: str = "template-drift.json",
           fail_on: str = "error", output: str = "text", extra: list[str] | None = None,
           env: dict[str, str] | None = None,
           labels: list[str] | None = None) -> subprocess.CompletedProcess[bytes]:
    roots = [templates] if isinstance(templates, Path) else templates
    args = CLI + ["--workspace", str(workspace)]
    for index, root in enumerate(roots):
        label = labels[index] if labels else f"example/{root.name}"
        args += ["--template", f"{label}={root}"]
    args += ["--config", config_path, "--fail-on", fail_on, "--format", output]
    args += extra or []
    run_env = os.environ.copy()
    run_env.update(env or {})
    return subprocess.run(args, cwd=ROOT, env=run_env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=False)

def decoded(test: unittest.TestCase, completed: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
    if completed.returncode == 2:
        test.assertEqual(completed.stdout, b"")
        match = ERROR.fullmatch(completed.stderr.decode().rstrip("\n"))
        test.assertIsNotNone(match, completed.stderr)
        assert match
        return {"status": "ERROR", "code": match.group(1), "message": match.group(2), "path": match.group(3)}
    test.assertEqual(completed.stderr, b"")
    lines = completed.stdout.decode().splitlines()
    summary = SUMMARY.fullmatch(lines[-1])
    test.assertIsNotNone(summary, completed.stdout)
    findings = []
    for line in lines[:-1]:
        match = FINDING.fullmatch(line)
        test.assertIsNotNone(match, line)
        assert match
        severity, path, mode, kind, template, message = match.groups()
        findings.append({"severity": severity, "path": path, "mode": mode, "kind": kind,
                         "template": template, "message": message})
    return {"status": summary.group(1), "findings": findings,
            "count": int(summary.group(2) or 0), "fixable": int(summary.group(3) or 0)}

class CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.template = self.root / "template-one"
        self.workspace.mkdir(); self.template.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def put(root: Path, relative: str, data: bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def configure(self, checks: list[dict[str, object]], root: Path | None = None) -> None:
        data = json.dumps(config(checks), separators=(",", ":")) + "\n"
        self.put(root or self.template, "template-drift.json", data.encode())

    def invoke(self, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return invoke(self.workspace, self.template, **kwargs)

    def result(self, completed: subprocess.CompletedProcess[bytes] | None = None) -> dict[str, object]:
        return decoded(self, completed or self.invoke())
