from __future__ import annotations
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from .support import check, config, decoded, invoke

class GitPatchTests(unittest.TestCase):
    @staticmethod
    def put(root: Path, relative: str, data: bytes) -> None:
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)

    @unittest.skipUnless(shutil.which("git"), "git is required for the real patch proof")
    def test_patch_applies_and_second_run_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); workspace = root / "workspace"; template = root / "template"
            workspace.mkdir(); template.mkdir()
            checks = [
                check("bytes_equal", source="src/change", target="dst/change"),
                check("must_exist", source="src/create", target="dst/create"),
                check("must_be_absent", path="dst/delete"),
                check("head_lines_equal", source="src/head", target="dst/head", head_lines=2),
            ]
            self.put(template, "template-drift.json", (json.dumps(config(checks), separators=(",", ":")) + "\n").encode())
            for path, data in {"src/change": b"new\n", "src/create": b"created\n", "src/head": b"one\ntwo\nignore\n"}.items(): self.put(template, path, data)
            for path, data in {"dst/change": b"old\n", "dst/delete": b"delete\n", "dst/head": b"one\nwrong\ntail\n"}.items(): self.put(workspace, path, data)
            patch = invoke(workspace, template, output="patch")
            self.assertEqual((patch.returncode, patch.stderr), (1, b""))
            patch_path = root / "change.patch"; patch_path.write_bytes(patch.stdout)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "apply", "--check", str(patch_path)], check=True)
            subprocess.run(["git", "-C", str(workspace), "apply", str(patch_path)], check=True)
            after = invoke(workspace, template)
            self.assertEqual((after.returncode, decoded(self, after)["status"]), (0, "PASS"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
