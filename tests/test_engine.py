from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import unittest
from workflow_template_drift import engine
from .support import CLI, CliCase, check, decoded, invoke

class EngineBehaviorTests(CliCase):
    def test_four_modes_and_patch(self) -> None:
        self.configure([
            check("bytes_equal", source="s/a", target="t/a"),
            check("must_exist", source="s/new", target="t/new", severity="warning"),
            check("head_lines_equal", source="s/head", target="t/head", head_lines=2),
            check("must_be_absent", path="t/old"),
        ])
        for path, data in {"s/a": b"new\n", "s/new": b"create\n", "s/head": b"one\ntwo\nignored\n"}.items():
            self.put(self.template, path, data)
        for path, data in {"t/a": b"old\n", "t/head": b"one\nwrong\ntail\n", "t/old": b"remove\n"}.items():
            self.put(self.workspace, path, data)
        result = self.result()
        self.assertEqual((self.invoke().returncode, result["status"], result["count"], result["fixable"]), (1, "FAIL", 4, 4))
        self.assertEqual({f["mode"] for f in result["findings"]}, {"bytes_equal", "must_exist", "head_lines_equal", "must_be_absent"})
        patch = self.invoke(output="patch")
        self.assertEqual((patch.returncode, patch.stderr), (1, b""))
        self.assertEqual(patch.stdout.count(b"diff --git "), 4)

    def test_pass_and_warning_threshold(self) -> None:
        self.configure([check("must_exist", path="present")])
        self.put(self.workspace, "present", b"anything\x00")
        self.assertEqual((self.invoke().returncode, self.result()["status"]), (0, "PASS"))
        self.configure([check("must_exist", path="missing", severity="warning")])
        self.assertEqual((self.invoke().returncode, self.result()["status"]), (0, "WARNING"))
        warning = self.invoke(fail_on="warning")
        self.assertEqual((warning.returncode, self.result(warning)["status"]), (1, "FAIL"))

    def test_multi_template_sort_and_disjointness(self) -> None:
        second = self.root / "template-two"; second.mkdir()
        self.configure([check("must_be_absent", path="z")])
        self.configure([check("must_be_absent", path="a")], second)
        self.put(self.workspace, "a", b"a"); self.put(self.workspace, "z", b"z")
        completed = invoke(self.workspace, [self.template, second])
        result = decoded(self, completed)
        self.assertEqual([f["path"] for f in result["findings"]], ["a", "z"])
        self.configure([check("must_exist", path="z/child")], second)
        failed = invoke(self.workspace, [self.template, second])
        error = decoded(self, failed)
        self.assertEqual((failed.returncode, error["code"]), (2, "invalid_config"))
        self.assertIn("template-one", error["message"]); self.assertIn("template-two", error["message"])

    def test_duplicate_targets_in_one_template(self) -> None:
        self.configure([check("must_exist", path="x"), check("must_be_absent", path="x")])
        self.assertEqual(self.result()["code"], "invalid_config")

    def test_path_traversal_and_git_components(self) -> None:
        for path in ("../x", "/tmp/x", "x//y", "x/.git/config", "x/.GiT../config", "x\\y", "café"):
            with self.subTest(path=path):
                self.configure([check("must_be_absent", path=path)])
                self.assertEqual(self.result()["code"], "invalid_config")

    def test_symlink_components_are_not_followed(self) -> None:
        self.configure([check("must_be_absent", path="linked/file")])
        (self.workspace / "real").mkdir(); (self.workspace / "linked").symlink_to("real", target_is_directory=True)
        completed = self.invoke()
        self.assertEqual((completed.returncode, self.result(completed)["code"]), (2, "io_error"))

    def test_final_symlink_is_finding_not_read(self) -> None:
        self.configure([check("bytes_equal", source="source", target="target")])
        self.put(self.template, "source", b"same\n"); self.put(self.workspace, "elsewhere", b"same\n")
        (self.workspace / "target").symlink_to("elsewhere")
        completed = self.invoke()
        self.assertEqual((completed.returncode, self.result(completed)["fixable"]), (1, 0))
        self.assertEqual(self.invoke(output="patch").stdout, b"")

    def test_head_lines_text_safety(self) -> None:
        self.configure([check("head_lines_equal", source="source", target="target", head_lines=2)])
        self.put(self.template, "source", b"one\ntwo\n")
        for data in (b"\xef\xbb\xbfone\ntwo\n", b"one\n\x00two\n", b"one\n\xff\n"):
            self.put(self.workspace, "target", data)
            self.assertEqual(self.result()["findings"][0]["kind"], "target_malformed")

    def test_source_and_config_errors(self) -> None:
        self.configure([check("bytes_equal", source="missing", target="target")])
        self.assertEqual(self.result()["code"], "source_missing")
        (self.template / "template-drift.json").unlink()
        missing = self.invoke()
        self.assertEqual((missing.returncode, missing.stdout, self.result(missing)["code"]), (2, b"", "config_missing"))

    def test_strict_json_schema(self) -> None:
        invalid = [b'{"version":3,"version":3,"checks":[]}\n', b'{"version":3,"checks":[]}\n',
                   b'{"version":3,"checks":[{"mode":"unknown","path":"x"}]}\n',
                   b'{"version":3,"checks":[{"mode":"must_exist","path":"x","extra":1}]}\n']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.put(self.template, "template-drift.json", raw)
                self.assertEqual(self.result()["code"], "invalid_config")

    def test_resource_limits(self) -> None:
        self.put(self.template, "template-drift.json", b" " * (engine.MAX_CONFIG_BYTES + 1))
        self.assertEqual(self.result()["code"], "resource_limit")
        self.configure([check("head_lines_equal", path="x", head_lines=engine.MAX_HEAD_LINES + 1)])
        self.assertEqual(self.result()["code"], "resource_limit")

    def test_file_and_eight_mib_output_caps(self) -> None:
        self.configure([check("bytes_equal", source="large", target="large")])
        path = self.put(self.template, "large", b"")
        with path.open("wb") as stream:
            stream.truncate(engine.MAX_FILE_BYTES + 1)
        self.assertEqual(self.result()["code"], "resource_limit")
        self.put(self.template, "large", b"a" * 4_300_000)
        self.put(self.workspace, "large", b"b" * 4_300_000)
        completed = self.invoke()
        self.assertEqual((completed.returncode, self.result(completed)["code"]), (2, "resource_limit"))

    def test_usage_abi_and_removed_options(self) -> None:
        for args in ([], ["--format", "json"], ["--source", "x"]):
            completed = subprocess.run(CLI + args, cwd=Path(__file__).parents[1], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, check=False)
            error = decoded(self, completed)
            self.assertEqual((completed.returncode, completed.stdout, error["code"]), (2, b"", "invalid_usage"))
        base = CLI + ["--workspace", str(self.workspace)]
        invalid = [str(self.template), f"invalid={self.template}", "owner/repo="]
        duplicate = ["--template", f"Owner/Repo={self.template}",
                     "--template", f"owner/repo={self.template}"]
        for args in (["--template", value] for value in invalid):
            with self.subTest(template=args[-1]):
                completed = subprocess.run(base + args, cwd=Path(__file__).parents[1], stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, check=False)
                self.assertEqual((completed.returncode, completed.stdout, completed.stderr),
                                 (2, b"", b"template-drift: error: invalid_usage: invalid command-line arguments\n"))
        completed = subprocess.run(base + duplicate, cwd=Path(__file__).parents[1], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        self.assertEqual((completed.returncode, completed.stdout, completed.stderr),
                         (2, b"", b"template-drift: error: invalid_usage: invalid command-line arguments\n"))
        self.configure([check("must_be_absent", path="mismatch")])
        self.put(self.workspace, "mismatch", b"present\n")
        self.put(self.template, ".git/config", b"[remote \"origin\"]\n\turl = https://github.com/other/origin.git\n")
        mismatch = invoke(self.workspace, self.template, labels=["chosen/label"])
        self.assertEqual((mismatch.returncode, decoded(self, mismatch)["findings"][0]["template"]),
                         (1, "chosen/label"))

    def test_config_applies_to_every_template(self) -> None:
        second = self.root / "second"; second.mkdir()
        self.configure([check("must_be_absent", path="x")])
        self.configure([check("must_be_absent", path="y")], second)
        missing = invoke(self.workspace, [self.template, second], config_path="other.json")
        self.assertEqual(decoded(self, missing)["code"], "config_missing")

    def test_no_final_newline_patch(self) -> None:
        self.configure([check("bytes_equal", source="s", target="t")])
        self.put(self.template, "s", b"desired\n"); self.put(self.workspace, "t", b"actual")
        self.assertIn(b"\\ No newline at end of file", self.invoke(output="patch").stdout)

    def test_environment_does_not_change_bytes(self) -> None:
        self.configure([check("must_be_absent", path="x")]); self.put(self.workspace, "x", b"x")
        baseline = self.invoke().stdout
        for env in ({"TZ": "Pacific/Kiritimati"}, {"LANG": "C", "NO_COLOR": "1"}, {"GITHUB_ACTIONS": "true"}):
            self.assertEqual(self.invoke(env=env).stdout, baseline)

if __name__ == "__main__":
    unittest.main(verbosity=2)
