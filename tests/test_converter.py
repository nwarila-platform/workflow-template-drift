from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from workflow_template_drift import engine

from .support import ARTIFACTS, ROOT


TOOL = ROOT / "tools" / "migrate_manifest.py"


class ConverterTests(unittest.TestCase):
    records: dict[str, dict[str, object]] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "converter-runs.json").write_text(
            json.dumps(cls.records, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )

    def run_converter(self, name: str, manifest: Path) -> tuple[subprocess.CompletedProcess[bytes], dict[str, object]]:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ARTIFACTS / "tmp") as cwd:
            before = list(Path(cwd).iterdir())
            completed = subprocess.run(
                ["python3.12", str(TOOL), str(manifest)],
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            after = list(Path(cwd).iterdir())
        self.assertEqual(before, after)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertTrue(completed.stdout.endswith(b"\n"))
        document = json.loads(completed.stdout)
        engine._schema_config(document)
        review_lines = [line for line in completed.stderr.decode().splitlines() if ": review required (" in line]
        record = {
            "exit": completed.returncode,
            "input_bytes": manifest.stat().st_size,
            "input_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "converted_checks": len(document["checks"]),
            "stdout_bytes": len(completed.stdout),
            "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
            "stderr_bytes": len(completed.stderr),
            "stderr_lines": len(completed.stderr.splitlines()),
            "review_summary_count": int(completed.stderr.decode().splitlines()[-1].split()[2]),
            "review_path_lines": len(review_lines),
            "wrote_files": False,
        }
        self.records[name] = record
        (ARTIFACTS / f"converter-{name}.stdout.json").write_bytes(completed.stdout)
        (ARTIFACTS / f"converter-{name}.stderr.txt").write_bytes(completed.stderr)
        return completed, document

    def test_v1_real_nwarila_github_manifest(self) -> None:
        manifest = ROOT / "fixtures" / "real" / "nwarila-github-baseline-manifest.json"
        completed, document = self.run_converter("nwarila-github-v1", manifest)
        self.assertEqual((len(document["checks"]), self.records["nwarila-github-v1"]["review_path_lines"]), (29, 0))
        self.assertEqual(completed.stderr.decode().splitlines(), [
            "converted: 29 v1 files -> bytes_equal",
            "review required: 0 scaffold_starter paths",
        ])
        self.assertTrue(all(item["mode"] == "bytes_equal" and item["severity"] == "error" for item in document["checks"]))

    def test_v2_real_terraform_runner_template_manifest(self) -> None:
        manifest = ROOT / "fixtures" / "real" / "terraform-runner-template-baseline-manifest.json"
        completed, document = self.run_converter("terraform-runner-template-v2", manifest)
        lines = completed.stderr.decode().splitlines()
        self.assertEqual((len(document["checks"]), len(lines), self.records["terraform-runner-template-v2"]["review_path_lines"]), (2, 97, 95))
        self.assertEqual(lines[0], "converted: 2 v2 byte_identical -> bytes_equal")
        self.assertEqual(lines[-1], "review required: 95 scaffold_starter paths")
        source = json.loads(manifest.read_bytes())
        expected = [
            f'{item["target"]}: review required (must_exist | bytes_equal | omit | conflict)'
            for item in source["scaffold_starter"]
        ]
        self.assertEqual(lines[1:-1], expected)

    def test_converter_uses_path_shorthand_and_explicit_renames(self) -> None:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ARTIFACTS / "tmp") as temporary:
            manifest = Path(temporary) / "v1.json"
            manifest.write_text(json.dumps({
                "version": "1",
                "files": [
                    {"source": "same", "target": "same"},
                    {"source": "old", "target": "new"},
                ],
            }), encoding="utf-8")
            completed = subprocess.run(
                ["python3.12", str(TOOL), str(manifest)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            rejected = []
            for name, files in (
                (
                    "overlap",
                    [
                        {"source": "s/parent", "target": "a"},
                        {"source": "s/child", "target": "a/b"},
                    ],
                ),
                ("git-path", [{"source": "s/file", "target": ".Git../config"}]),
            ):
                candidate = Path(temporary) / f"{name}.json"
                candidate.write_text(json.dumps({"version": "1", "files": files}), encoding="utf-8")
                rejected.append(subprocess.run(
                    ["python3.12", str(TOOL), str(candidate)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                ))
            too_many = [
                {"source": f"s/{index}", "target": f"t/{index}"}
                for index in range(engine.MAX_CHECKS + 1)
            ]
            count_rejected = []
            for name, value in (
                ("v1", {"version": "1", "files": too_many}),
                ("v2", {"version": "2", "byte_identical": too_many, "scaffold_starter": []}),
            ):
                count_manifest = Path(temporary) / f"too-many-{name}.json"
                count_manifest.write_text(json.dumps(value), encoding="ascii")
                count_rejected.append(subprocess.run(
                    ["python3.12", str(TOOL), str(count_manifest)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                ))
            def long_path(prefix: str, fill: str) -> str:
                result = prefix
                while len(result) < 1024:
                    width = min(200, 1024 - len(result) - 1)
                    result += "/" + fill * width
                return result

            long_mappings = []
            for index in range(512):
                long_mappings.append({
                    "source": long_path(f"s{index:04d}", "a"),
                    "target": long_path(f"t{index:04d}", "b"),
                })
            size_manifest = Path(temporary) / "too-large.json"
            size_manifest.write_text(
                json.dumps({"version": "1", "files": long_mappings}),
                encoding="ascii",
            )
            size_rejected = subprocess.run(
                ["python3.12", str(TOOL), str(size_manifest)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        document = json.loads(completed.stdout)
        self.assertEqual(document["checks"], [
            {"mode": "bytes_equal", "path": "same", "severity": "error"},
            {"mode": "bytes_equal", "source": "old", "target": "new", "severity": "error"},
        ])
        for failed in rejected:
            self.assertEqual((failed.returncode, failed.stdout), (2, b""))
        for failed in count_rejected:
            self.assertEqual(
                (failed.returncode, failed.stdout, failed.stderr),
                (2, b"", b"error: conversion would exceed v3 4096-check limit\n"),
            )
        self.assertEqual(
            (size_rejected.returncode, size_rejected.stdout, size_rejected.stderr),
            (2, b"", b"error: conversion would exceed v3 1048576-byte config limit\n"),
        )

    def test_empty_legacy_manifest_cannot_produce_valid_nonempty_v3(self) -> None:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ARTIFACTS / "tmp") as temporary:
            manifest = Path(temporary) / "empty-v2.json"
            manifest.write_text(
                '{"version":"2","byte_identical":[],"scaffold_starter":[]}\n',
                encoding="ascii",
            )
            completed = subprocess.run(
                ["python3.12", str(TOOL), str(manifest)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            b"error: conversion would produce an invalid empty v3 checks array\n",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
