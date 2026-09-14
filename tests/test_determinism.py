from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from .support import ARTIFACTS, ROOT, decode, invoke


class DeterminismTests(unittest.TestCase):
    def test_eight_variant_hostile_environment_matrix_matches_golden(self) -> None:
        source = ROOT / "fixtures" / "determinism" / "source"
        workspace = ROOT / "fixtures" / "determinism" / "workspace"
        golden = (ROOT / "fixtures" / "goldens" / "determinism.stdout.json").read_bytes()
        variants = {
            "baseline-1": {},
            "baseline-2": {},
            "no-color": {"NO_COLOR": "1"},
            "timezone": {"TZ": "Pacific/Kiritimati"},
            "hostile-python": {
                "PYTHONPATH": "/hostile/pythonpath",
                "PYTHONHOME": "/hostile/pythonhome",
                "PYTHONSTARTUP": "/hostile/startup.py",
            },
            "hostile-locale": {"LANG": "C", "LC_ALL": "C"},
            "hostile-github": {
                "GITHUB_ACTIONS": "true",
                "GITHUB_ENV": "/hostile/github-env",
                "GITHUB_OUTPUT": "/hostile/github-output",
                "GITHUB_WORKSPACE": "/hostile/workspace",
            },
            "hostile-combined": {
                "NO_COLOR": "0",
                "TZ": "Etc/GMT+12",
                "PYTHONPATH": "/hostile/pythonpath",
                "PYTHONHOME": "/hostile/pythonhome",
                "PYTHONSTARTUP": "/hostile/startup.py",
                "LANG": "C",
                "LC_ALL": "C",
                "GITHUB_ACTIONS": "true",
                "GITHUB_ENV": "/hostile/github-env",
                "GITHUB_OUTPUT": "/hostile/github-output",
                "GITHUB_WORKSPACE": "/hostile/workspace",
                "WORKFLOW_TEMPLATE_DRIFT_SURPRISE": "ignored",
            },
        }
        records: dict[str, dict[str, object]] = {}
        for name, overrides in variants.items():
            with self.subTest(name=name):
                completed = invoke(workspace, source, overrides=overrides)
                result = decode(self, completed)
                self.assertEqual((completed.returncode, result["status"]), (1, "FAIL"))
                self.assertEqual(completed.stdout, golden)
                records[name] = {
                    "exit": completed.returncode,
                    "stdout_bytes": len(completed.stdout),
                    "stdout_sha256": hashlib.sha256(completed.stdout).hexdigest(),
                    "stderr_bytes": len(completed.stderr),
                }
        self.assertEqual(len(records), 8)
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "determinism-matrix.json").write_text(
            json.dumps(records, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
