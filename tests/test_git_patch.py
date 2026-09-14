from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from .support import ARTIFACTS, ROOT, check, config, decode, invoke


class GitPatchTests(unittest.TestCase):
    def write(self, root: Path, relative: str, data: bytes) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def git(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", *args],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    @unittest.skipUnless(
        shutil.which("git") is not None,
        "git is required for the real-git apply proof",
    )
    def test_real_git_checks_applies_and_cleans_seven_fixes(self) -> None:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ARTIFACTS / "tmp") as temporary:
            case = Path(temporary)
            repo = case / "repo"
            source = case / "source"
            repo.mkdir()
            source.mkdir()
            manifest = config([
                check("bytes_equal", source="template/crlf.txt", target="case/crlf.txt"),
                check("bytes_equal", source="template/no-final.txt", target="case/no-final.txt"),
                check("must_exist", source="template/create.txt", target="case/create.txt"),
                check("must_exist", source="template/create-empty.txt", target="case/create-empty.txt"),
                check("must_be_absent", path="case/delete.txt"),
                check("must_be_absent", path="case/delete-empty.txt"),
                check("head_lines_equal", source="template/head.txt", target="case/head.txt", head_lines=2),
            ])
            self.write(source, "drift.json", (json.dumps(manifest, separators=(",", ":")) + "\n").encode())
            self.write(source, "template/crlf.txt", b"alpha\nbeta\n")
            self.write(source, "template/no-final.txt", b"desired with final LF\n")
            self.write(source, "template/create.txt", b"created exactly\n")
            self.write(source, "template/create-empty.txt", b"")
            self.write(source, "template/head.txt", b"source one\nsource two\nignored source tail\n")
            self.write(repo, "case/crlf.txt", b"alpha\r\nbeta\r\n")
            self.write(repo, "case/no-final.txt", b"observed without final LF")
            self.write(repo, "case/delete.txt", b"delete exactly\n")
            self.write(repo, "case/delete-empty.txt", b"")
            self.write(repo, "case/head.txt", b"consumer one\nconsumer two\nconsumer tail\r\n")

            before = invoke(repo, source)
            result = decode(self, before)
            self.assertEqual((before.returncode, result["status"], len(result["findings"])), (1, "FAIL", 7))
            self.assertTrue(all(item["fixable"] for item in result["findings"]))
            patch = result["patch"].encode("utf-8")
            patch_path = case / "remediation.patch"
            patch_path.write_bytes(patch)

            initialized = self.git("-c", "core.autocrlf=false", "init", "-q", str(repo))
            configured = self.git("-C", str(repo), "config", "core.autocrlf", "false")
            checked = self.git("-C", str(repo), "apply", "--check", str(patch_path))
            applied = self.git("-C", str(repo), "apply", str(patch_path))
            for completed in (initialized, configured, checked, applied):
                self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))

            expected = {
                "case/crlf.txt": b"alpha\nbeta\n",
                "case/no-final.txt": b"desired with final LF\n",
                "case/create.txt": b"created exactly\n",
                "case/create-empty.txt": b"",
                "case/head.txt": b"source one\nsource two\nconsumer tail\r\n",
            }
            self.assertTrue(all((repo / path).read_bytes() == data for path, data in expected.items()))
            self.assertFalse((repo / "case/delete.txt").exists())
            self.assertFalse((repo / "case/delete-empty.txt").exists())
            after = invoke(repo, source)
            after_result = decode(self, after)
            self.assertEqual((after.returncode, after_result["status"], after_result["findings"]), (0, "PASS", []))

            version = self.git("--version")
            self.assertEqual(version.returncode, 0)
            record = {
                "git_version": version.stdout.decode().strip(),
                "before_exit": before.returncode,
                "before_findings": len(result["findings"]),
                "before_fixable": sum(item["fixable"] for item in result["findings"]),
                "patch_bytes": len(patch),
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
                "git_init_exit": initialized.returncode,
                "git_apply_check_exit": checked.returncode,
                "git_apply_exit": applied.returncode,
                "post_apply_exact_bytes": True,
                "after_exit": after.returncode,
                "after_status": after_result["status"],
                "after_findings": len(after_result["findings"]),
            }
            (ARTIFACTS / "git-patch.json").write_text(
                json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="ascii",
            )
            (ARTIFACTS / "remediation.patch").write_bytes(patch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
