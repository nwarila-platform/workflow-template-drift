"""End-to-end tests for the checker.

Every test builds a small template and a small repository in a temporary directory, runs the real
command line against them, and compares the exact report and exit status.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


class CheckerTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.template = Path(scratch.name, "template")
        self.repository = Path(scratch.name, "repository")
        self.template.mkdir()
        self.repository.mkdir()

    def write(self, root, path, content):
        """Create one file beneath the template or the repository."""
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)

    def manifest(self, *checks, root=None):
        """Write a template's manifest holding the given checks."""
        self.write(root or self.template, "template-drift.json", json.dumps({"version": 3, "checks": checks}))

    def command(self, *options, templates=None):
        """Build the command line that checks the repository against the template."""
        command = [sys.executable, "-m", "workflow_template_drift", "--workspace", str(self.repository)]
        for label, directory in (templates or {"acme/template": self.template}).items():
            command += ["--template", f"{label}={directory}"]
        return [*command, *options]

    def check(self, *options, templates=None):
        """Run the command line and return its exit status, its report and its error output."""
        command = self.command(*options, templates=templates)
        done = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True, check=False)
        return done.returncode, done.stdout, done.stderr

    def assert_cannot_run(self, message):
        """The check stops with status 2, prints no report, and explains itself in one line."""
        self.assertEqual(self.check(), (2, "", f"template-drift: error: {message}\n"))

    # --- the four modes ---------------------------------------------------------------------- #

    def test_a_repository_that_follows_its_template_passes(self):
        self.manifest(
            {"mode": "bytes_equal", "path": "lint.toml"},
            {"mode": "head_lines_equal", "path": "pipeline.yaml", "head_lines": 1},
            {"mode": "must_exist", "path": "README.md"},
            {"mode": "must_be_absent", "path": "legacy.cfg"},
        )
        self.write(self.template, "lint.toml", "strict = true\n")
        self.write(self.repository, "lint.toml", "strict = true\n")
        self.write(self.template, "pipeline.yaml", "stages:\n")
        self.write(self.repository, "pipeline.yaml", "stages:\n  - deploy\n")
        self.write(self.repository, "README.md", "anything at all\n")
        self.assertEqual(
            self.check(), (0, "template-drift: PASS (1 template, 4 checks, 0 errors, 0 warnings)\n", "")
        )

    def test_bytes_equal_reports_a_file_that_differs(self):
        self.manifest({"mode": "bytes_equal", "path": "lint.toml"})
        self.write(self.template, "lint.toml", "strict = true\n")
        self.write(self.repository, "lint.toml", "strict = false\n")
        self.assertEqual(
            self.check(),
            (
                1,
                "error: lint.toml: bytes_equal/content_mismatch (template acme/template): "
                "target content differs from the pinned template\n"
                "template-drift: FAIL (1 template, 1 check, 1 error, 0 warnings)\n",
                "",
            ),
        )

    def test_source_and_target_compare_two_differently_named_files(self):
        self.manifest({"mode": "bytes_equal", "source": "shared/lint.toml", "target": "config/lint.toml"})
        self.write(self.template, "shared/lint.toml", "strict = true\n")
        self.write(self.repository, "config/lint.toml", "strict = true\n")
        self.assertEqual(self.check()[0], 0)
        self.write(self.repository, "config/lint.toml", "strict = false\n")
        self.assertIn("error: config/lint.toml: bytes_equal/content_mismatch", self.check()[1])

    def test_head_lines_equal_compares_only_the_leading_lines(self):
        self.manifest({"mode": "head_lines_equal", "path": "pipeline.yaml", "head_lines": 2})
        self.write(self.template, "pipeline.yaml", "stages:\n  - test\n  - never compared\n")
        for content, status in (
            ("stages:\n  - test\n", 0),  # exactly the head
            ("stages:\n  - test\n  - deploy\n", 0),  # the head, then the repository's own lines
            ("stages:\n  - skip\n  - deploy\n", 1),  # a changed line inside the head
            ("stages:\n", 1),  # shorter than the head
            ("stages:\n  - test", 1),  # the head's last line has lost its newline
        ):
            with self.subTest(content=content):
                self.write(self.repository, "pipeline.yaml", content)
                self.assertEqual(self.check()[0], status)

    def test_only_a_line_feed_ends_a_line(self):
        self.manifest({"mode": "head_lines_equal", "path": "notes.txt", "head_lines": 1})
        self.write(self.template, "notes.txt", "one\rstill line one\nline two\n")
        self.write(self.repository, "notes.txt", "one\rCHANGED\nline two\n")
        self.assertIn("error: notes.txt: head_lines_equal/content_mismatch", self.check()[1])

    def test_must_exist_needs_a_regular_file(self):
        self.manifest({"mode": "must_exist", "path": "README.md"})
        self.assertIn("error: README.md: must_exist/target_missing", self.check()[1])
        (self.repository / "README.md").mkdir()
        self.assertIn("error: README.md: must_exist/target_not_regular", self.check()[1])

    def test_must_be_absent_reports_a_file_that_exists(self):
        self.manifest({"mode": "must_be_absent", "path": "legacy.cfg"})
        self.write(self.repository, "legacy.cfg", "still here\n")
        self.assertIn("error: legacy.cfg: must_be_absent/target_present", self.check()[1])

    def test_a_compared_file_that_is_missing_or_is_a_directory_is_reported(self):
        self.manifest({"mode": "bytes_equal", "path": "lint.toml"})
        self.write(self.template, "lint.toml", "strict = true\n")
        self.assertIn("error: lint.toml: bytes_equal/target_missing", self.check()[1])
        (self.repository / "lint.toml").mkdir()
        self.assertIn("error: lint.toml: bytes_equal/target_not_regular", self.check()[1])

    # --- symbolic links ---------------------------------------------------------------------- #

    def test_a_symbolic_link_cannot_stand_in_for_a_governed_file(self):
        self.manifest({"mode": "bytes_equal", "path": "lint.toml"})
        self.write(self.template, "lint.toml", "strict = true\n")
        self.write(self.repository, "elsewhere.toml", "strict = true\n")
        (self.repository / "lint.toml").symlink_to("elsewhere.toml")
        self.assertIn("error: lint.toml: bytes_equal/target_not_regular", self.check()[1])

    def test_a_symbolic_link_above_a_governed_path_stops_the_check(self):
        self.manifest({"mode": "must_be_absent", "path": "config/legacy.cfg"})
        (self.repository / "elsewhere").mkdir()
        (self.repository / "config").symlink_to("elsewhere")
        self.assert_cannot_run("config/legacy.cfg lies beneath a symbolic link, and links are never followed")

    def test_a_symbolic_link_counts_as_present(self):
        self.manifest({"mode": "must_be_absent", "path": "legacy.cfg"})
        (self.repository / "legacy.cfg").symlink_to("nowhere")
        self.assertIn("error: legacy.cfg: must_be_absent/target_present", self.check()[1])

    # --- severities, and several templates --------------------------------------------------- #

    def test_a_warning_is_reported_but_fails_only_when_asked_to(self):
        self.manifest({"mode": "must_exist", "path": "SECURITY.md", "severity": "warning"})
        finding = "warning: SECURITY.md: must_exist/target_missing (template acme/template): target is missing\n"
        self.assertEqual(
            self.check(),
            (0, finding + "template-drift: WARNING (1 template, 1 check, 0 errors, 1 warning)\n", ""),
        )
        self.assertEqual(
            self.check("--fail-on", "warning"),
            (1, finding + "template-drift: FAIL (1 template, 1 check, 0 errors, 1 warning)\n", ""),
        )

    def test_findings_from_several_templates_are_sorted_by_path(self):
        second = self.template.parent / "second"
        self.manifest({"mode": "must_exist", "path": "b.txt"}, {"mode": "must_exist", "path": "d.txt"})
        self.manifest({"mode": "must_exist", "path": "c.txt"}, {"mode": "must_exist", "path": "a.txt"}, root=second)
        status, report, _ = self.check(templates={"acme/one": self.template, "acme/two": second})
        self.assertEqual(status, 1)
        self.assertEqual(
            report.splitlines(),
            [
                "error: a.txt: must_exist/target_missing (template acme/two): target is missing",
                "error: b.txt: must_exist/target_missing (template acme/one): target is missing",
                "error: c.txt: must_exist/target_missing (template acme/two): target is missing",
                "error: d.txt: must_exist/target_missing (template acme/one): target is missing",
                "template-drift: FAIL (2 templates, 4 checks, 4 errors, 0 warnings)",
            ],
        )

    def test_two_templates_cannot_govern_the_same_path(self):
        second = self.template.parent / "second"
        self.manifest({"mode": "must_exist", "path": "README.md"})
        self.manifest({"mode": "must_be_absent", "path": "README.md"}, root=second)
        status, report, error = self.check(templates={"acme/one": self.template, "acme/two": second})
        self.assertEqual(
            (status, report, error),
            (2, "", "template-drift: error: README.md has two rules: one from acme/one, one from acme/two\n"),
        )

    # --- everything that stops the check, with status 2 -------------------------------------- #

    def test_a_template_without_a_manifest_stops_the_check(self):
        self.assert_cannot_run("acme/template: there is no template-drift.json file")

    def test_a_malformed_manifest_stops_the_check(self):
        for content, message in (
            ("{", "is not valid JSON"),
            ('{"version": 3, "version": 3, "checks": []}', "the key 'version' appears twice"),
            ('{"version": 3, "checks": [], "extra": 1}', "must hold exactly the keys version and checks"),
            ('{"version": 2, "checks": []}', "version must be 3"),
            ('{"version": 3.0, "checks": [{"mode": "must_exist", "path": "x"}]}', "version must be 3"),
            ('{"version": 3, "checks": []}', "checks must be a non-empty list"),
        ):
            with self.subTest(content=content):
                self.write(self.template, "template-drift.json", content)
                status, report, error = self.check()
                self.assertEqual((status, report), (2, ""))
                self.assertIn(message, error)

    def test_a_malformed_check_stops_the_check(self):
        self.write(self.template, "lint.toml", "one line\n")
        for check, message in (
            ("not an object", "checks[0] must be an object"),
            ({"mode": "unknown", "path": "x"}, "mode must be one of"),
            ({"mode": "must_exist", "path": "x", "severity": "fatal"}, "severity must be error or warning"),
            ({"mode": "must_exist", "path": "x", "pth": "y"}, "must_exist takes path"),
            ({"mode": "must_exist", "source": "x", "target": "y"}, "must_exist takes path"),
            ({"mode": "bytes_equal", "path": "x", "target": "y"}, "bytes_equal takes path or source + target"),
            ({"mode": "head_lines_equal", "path": "lint.toml"}, "head_lines_equal takes head_lines + path"),
            ({"mode": "head_lines_equal", "path": "lint.toml", "head_lines": 0}, "head_lines must be a whole number"),
            ({"mode": "head_lines_equal", "path": "lint.toml", "head_lines": True}, "head_lines must be a whole number"),
            ({"mode": "head_lines_equal", "path": "lint.toml", "head_lines": 2}, "lint.toml has fewer than 2 lines"),
            ({"mode": "bytes_equal", "path": "absent.toml"}, "the template has no file absent.toml"),
        ):
            with self.subTest(check=check):
                self.manifest(check)
                status, report, error = self.check()
                self.assertEqual((status, report), (2, ""))
                self.assertIn(message, error)

    def test_a_path_that_could_leave_the_repository_stops_the_check(self):
        for path in ("../secret", "/etc/passwd", "a//b", "a/./b", "a/../b", "a\\b", "", "name with spaces", 7):
            with self.subTest(path=path):
                self.manifest({"mode": "must_be_absent", "path": path})
                status, report, error = self.check()
                self.assertEqual((status, report), (2, ""))
                self.assertIn("path must be a relative path", error)

    def test_a_missing_workspace_stops_the_check(self):
        self.manifest({"mode": "must_exist", "path": "README.md"})
        self.repository.rmdir()
        self.assert_cannot_run(f"workspace {self.repository} is not a directory")

    @unittest.skipIf(os.geteuid() == 0, "the administrator can read every file")
    def test_a_file_that_cannot_be_read_stops_the_check(self):
        self.manifest({"mode": "bytes_equal", "path": "lint.toml"})
        self.write(self.template, "lint.toml", "strict = true\n")
        self.write(self.repository, "lint.toml", "strict = true\n")
        (self.repository / "lint.toml").chmod(0)
        status, report, error = self.check()
        self.assertEqual((status, report), (2, ""))
        self.assertIn("template-drift: error: PermissionError", error)

    @unittest.skipUnless(Path("/dev/full").exists(), "needs the always-full device that Linux has")
    def test_a_report_that_cannot_be_written_is_not_mistaken_for_drift(self):
        self.manifest({"mode": "must_exist", "path": "README.md"})
        with open("/dev/full", "w") as full:
            done = subprocess.run(
                self.command(), cwd=PROJECT, stdout=full, stderr=subprocess.PIPE, text=True, check=False
            )
        self.assertEqual(
            (done.returncode, done.stderr),
            (2, "template-drift: error: OSError: [Errno 28] No space left on device\n"),
        )

    def test_wrong_arguments_stop_the_check(self):
        for arguments, message in (
            ([], "the following arguments are required: --workspace, --template"),
            (["--workspace", "."], "the following arguments are required: --template"),
            (["--workspace", ".", "--template", "no-owner=."], "expected OWNER/REPO=DIRECTORY"),
            (["--workspace", ".", "--template", "acme/template"], "expected OWNER/REPO=DIRECTORY"),
            (["--workspace", ".", "--template", "acme/template=.", "--format", "patch"], "invalid choice: 'patch'"),
            (["--work", ".", "--template", "acme/template=."], "the following arguments are required: --workspace"),
        ):
            with self.subTest(arguments=arguments):
                done = subprocess.run(
                    [sys.executable, "-m", "workflow_template_drift", *arguments],
                    cwd=PROJECT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual((done.returncode, done.stdout), (2, ""))
                self.assertIn(message, done.stderr)

    # --- the bundled example ----------------------------------------------------------------- #

    def test_the_bundled_example_produces_its_expected_report(self):
        example = PROJECT / "example"
        self.repository = example / "repository"
        self.assertEqual(
            self.check(templates={"example/template": example / "template"}),
            (1, (example / "expected-report.txt").read_text(), ""),
        )

    def test_the_readme_quotes_the_example_exactly(self):
        readme = (PROJECT / "README.md").read_text()
        self.assertIn((PROJECT / "example/expected-report.txt").read_text(), readme)
        self.assertIn((PROJECT / "example/template/template-drift.json").read_text(), readme)


if __name__ == "__main__":
    unittest.main()
