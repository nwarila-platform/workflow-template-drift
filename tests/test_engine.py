from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from workflow_template_drift import engine

from .support import ARTIFACTS, CLI, ROOT, CliCase, check, config, decode, runtime_env


class EngineTests(CliCase):
    def test_01_positive_all_modes_pass_and_fail(self) -> None:
        self.configure(config([
            check("bytes_equal", source="s/equal", target="t/equal"),
            check("bytes_equal", source="s/bytes", target="t/bytes"),
            check("must_exist", path="t/exists"),
            check("must_exist", source="s/inventory", target="t/missing", severity="warning"),
            check("head_lines_equal", source="s/head", target="t/head-pass", head_lines=2),
            check("head_lines_equal", source="s/head", target="t/head-fail", severity="warning", head_lines=2),
            check("must_be_absent", path="t/absent"),
            check("must_be_absent", path="t/present"),
        ]))
        for relative, data in {
            "s/equal": b"same\n",
            "s/bytes": b"expected\n",
            "s/inventory": b"starter\n",
            "s/head": b"one\ntwo\nsource tail\n",
        }.items():
            self.put(self.source, relative, data)
        for relative, data in {
            "t/equal": b"same\n",
            "t/bytes": b"actual\n",
            "t/exists": b"anything\x00",
            "t/head-pass": b"one\ntwo\nconsumer tail\n",
            "t/head-fail": b"one\nWRONG\ntail\n",
            "t/present": b"delete me\n",
        }.items():
            self.put(self.workspace, relative, data)
        result = self.result()
        self.assertEqual((result["status"], len(result["findings"])), ("FAIL", 4))
        self.assertEqual(
            {item["mode"] for item in result["findings"]},
            {"bytes_equal", "must_exist", "head_lines_equal", "must_be_absent"},
        )
        self.assertEqual(self.result(self.invoke("warning"))["status"], "FAIL")

    def test_02_warning_threshold_matrix(self) -> None:
        self.configure(config([check("must_exist", source="s/source", target="t/missing", severity="warning")]))
        self.put(self.source, "s/source", b"create\n")
        warning = self.result(self.invoke("error"))
        fail = self.result(self.invoke("warning"))
        self.assertEqual((warning["status"], self.invoke("error").returncode), ("WARNING", 0))
        self.assertEqual((fail["status"], self.invoke("warning").returncode), ("FAIL", 1))

    def test_03_clean_pass(self) -> None:
        self.configure(config([
            check("bytes_equal", path="same/b"),
            check("must_exist", path="t/e"),
            check("head_lines_equal", path="same/h", head_lines=1),
            check("must_be_absent", path="t/a"),
        ]))
        self.put(self.source, "same/b", b"")
        self.put(self.workspace, "same/b", b"")
        self.put(self.source, "same/h", b"first\ntail\n")
        self.put(self.workspace, "same/h", b"first\ndifferent\n")
        self.put(self.workspace, "t/e", b"\x00binary accepted by must_exist")
        completed = self.invoke()
        result = self.result(completed)
        self.assertEqual((result["status"], completed.returncode, result["findings"]), ("PASS", 0, []))

    def _single_target_shape(self, setup) -> dict[str, object]:
        self.configure(config([check("bytes_equal", source="s/source", target="t/item")]))
        self.put(self.source, "s/source", b"expected\n")
        setup()
        return self.result()

    def test_04_symlinked_final_target(self) -> None:
        self.put(self.workspace, "elsewhere", b"expected\n")
        result = self._single_target_shape(
            lambda: (self.workspace / "t").mkdir() or (self.workspace / "t/item").symlink_to("../elsewhere")
        )
        self.assertEqual(result["findings"][0]["details"]["target_type"], "symlink")

    def test_05_symlinked_intermediate_target(self) -> None:
        self.configure(config([check("must_be_absent", path="linked/item")]))
        (self.workspace / "real").mkdir()
        (self.workspace / "linked").symlink_to("real", target_is_directory=True)
        completed = self.invoke()
        result = self.result(completed)
        self.assertEqual((completed.returncode, result["error"]["code"]), (2, "io_error"))

    def test_06_directory_target(self) -> None:
        def setup() -> None:
            (self.workspace / "t/item").mkdir(parents=True)

        self.assertEqual(self._single_target_shape(setup)["findings"][0]["details"]["target_type"], "directory")

    def test_07_special_target(self) -> None:
        def setup() -> None:
            (self.workspace / "t").mkdir()
            os.mkfifo(self.workspace / "t/item")

        self.assertEqual(self._single_target_shape(setup)["findings"][0]["details"]["target_type"], "special")

    def _head_target(self, target_data: bytes) -> dict[str, object]:
        self.configure(config([check("head_lines_equal", source="s/head", target="t/head", head_lines=2)]))
        self.put(self.source, "s/head", b"one\ntwo\n")
        self.put(self.workspace, "t/head", target_data)
        return self.result()

    def test_08_crlf_vs_lf(self) -> None:
        finding = self._head_target(b"one\r\ntwo\r\n")["findings"][0]
        self.assertEqual((finding["kind"], finding["fixable"]), ("content_mismatch", True))

    def test_09_bom(self) -> None:
        self.assertEqual(self._head_target(b"\xef\xbb\xbfone\ntwo\n")["findings"][0]["kind"], "target_malformed")
        self.configure(config([check("head_lines_equal", source="s/bom", target="t/head", head_lines=1)]))
        self.put(self.source, "s/bom", b"\xef\xbb\xbfsource\n")
        completed = self.invoke()
        self.assertEqual((completed.returncode, self.result(completed)["error"]["code"]), (2, "invalid_source"))

    def test_10_nul(self) -> None:
        self.assertEqual(self._head_target(b"one\n\x00two\n")["findings"][0]["kind"], "target_malformed")

    def test_11_no_final_newline(self) -> None:
        self.configure(config([check("bytes_equal", source="s/file", target="t/file")]))
        self.put(self.source, "s/file", b"desired\n")
        self.put(self.workspace, "t/file", b"observed")
        self.assertIn("\\ No newline at end of file\n", self.result()["patch"])

    def test_12_binary_mismatch(self) -> None:
        self.configure(config([check("bytes_equal", source="s/file", target="t/file")]))
        source = b"\x00\xffsource"
        target = b"\x00\xfetarget!"
        self.put(self.source, "s/file", source)
        self.put(self.workspace, "t/file", target)
        finding = self.result()["findings"][0]
        self.assertFalse(finding["fixable"])
        self.assertEqual(finding["details"], {
            "binary": True,
            "first_unequal_offset": 1,
            "source_length": len(source),
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "target_length": len(target),
            "target_sha256": hashlib.sha256(target).hexdigest(),
        })

    def test_13_duplicate_json_members(self) -> None:
        self.configure(b'{"version":3,"version":3,"checks":[]}\n', raw=True)
        self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_14_non_json_constants(self) -> None:
        for constant in (b"NaN", b"Infinity", b"-Infinity"):
            with self.subTest(constant=constant):
                self.configure(b'{"version":3,"checks":[],"x":' + constant + b'}\n', raw=True)
                self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_15_unknown_mode(self) -> None:
        self.configure(config([check("surprise", path="t/file")]))
        self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_16_unknown_key_and_id_rejected(self) -> None:
        item = check("must_exist", path="t/file")
        item["id"] = "removed"
        self.configure(config([item]))
        self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_17_authoring_shape_missing_and_mixed_keys(self) -> None:
        cases = [
            {"mode": "bytes_equal", "target": "t/file"},
            {"mode": "bytes_equal", "source": "s/file"},
            {"mode": "bytes_equal"},
            {"mode": "bytes_equal", "path": "t/file", "source": "s/file", "target": "t/file"},
            {"mode": "must_be_absent", "source": "s/file", "target": "t/file"},
            {"mode": "head_lines_equal", "path": "t/file"},
        ]
        for item in cases:
            with self.subTest(item=item):
                self.configure(config([item]))
                self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_18_head_lines_range_and_type(self) -> None:
        for count in (0, True, 1.0, "1"):
            with self.subTest(count=count):
                self.configure(config([check("head_lines_equal", path="x", head_lines=count)]))
                self.assertEqual(self.result()["error"]["code"], "invalid_config")
        self.configure(config([check("head_lines_equal", path="x", head_lines=10001)]))
        self.assertEqual(self.result()["error"]["code"], "resource_limit")

    def test_19_path_grammar(self) -> None:
        invalid = (
            "/etc/passwd", "safe/../escape", "safe/./file", "safe\\escape", "café/file",
            "safe/*.txt", "safe/?.txt", "safe/[a].txt", "safe/a].txt", "safe/{a}.txt",
            "safe/a}.txt", "safe/!.txt", "safe//file", "safe/file/", "safe/a b",
            "safe/a\nfile", "a" * 1025,
        )
        for path in invalid:
            with self.subTest(path=path):
                self.configure(config([check("must_be_absent", path=path)]))
                self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_20_source_missing(self) -> None:
        self.configure(config([check("bytes_equal", source="s/nope", target="t/file")]))
        completed = self.invoke()
        self.assertEqual((completed.returncode, self.result(completed)["error"]["code"]), (2, "source_missing"))

    def test_21_config_missing(self) -> None:
        completed = self.invoke(config_path="no-config.json")
        self.assertEqual((completed.returncode, self.result(completed)["error"]["code"]), (2, "config_missing"))

    def test_22_source_preflight_before_targets(self) -> None:
        self.configure(config([
            check("must_be_absent", path="t/present"),
            check("bytes_equal", source="s/missing", target="t/other"),
        ]))
        self.put(self.workspace, "t/present", b"present")
        completed = self.invoke()
        result = self.result(completed)
        self.assertEqual((completed.returncode, result["error"]["code"]), (2, "source_missing"))
        self.assertNotIn("findings", result)

    def test_23_result_over_limit(self) -> None:
        self.configure(config([check("bytes_equal", source="s/huge", target="t/huge")]))
        self.put(self.source, "s/huge", b"a" * 4_300_000)
        self.put(self.workspace, "t/huge", b"b" * 4_300_000)
        completed = self.invoke()
        result = self.result(completed)
        self.assertEqual((completed.returncode, result["error"]["code"]), (2, "resource_limit"))

    def test_24_v1_runtime_rejected(self) -> None:
        self.configure({"version": "1", "files": [{"source": "s", "target": "t"}]})
        self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_25_v2_runtime_rejected(self) -> None:
        self.configure({"version": "2", "byte_identical": [], "scaffold_starter": []})
        self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_26_non_applyable_target_sets_are_rejected(self) -> None:
        duplicate = [check("must_be_absent", path="same"), check("must_be_absent", path="same", severity="warning")]
        self.configure(config(duplicate))
        self.assertEqual(self.result()["error"]["code"], "invalid_config")
        self.configure(config([check("must_be_absent", path="same"), check("must_exist", path="same")]))
        self.assertEqual(self.result()["error"]["code"], "invalid_config")
        for invalid in (
            [check("must_exist", source="s/parent", target="a"), check("must_exist", source="s/child", target="a/b")],
            [check("must_exist", source="s/child", target="a/b"), check("must_exist", source="s/parent", target="a")],
            [check("must_be_absent", path=".git/config")],
            [check("must_be_absent", path="x/.GiT../config")],
        ):
            with self.subTest(invalid=invalid):
                self.configure(config(invalid))
                self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_27_usage_error_json_only(self) -> None:
        completed = subprocess.run(
            CLI,
            cwd=ROOT,
            env=runtime_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        result = decode(self, completed)
        self.assertEqual((completed.returncode, result["error"]["code"]), (2, "invalid_usage"))

    def test_28_finding_order_and_no_identity_fields(self) -> None:
        self.configure(config([
            check("must_be_absent", path="z/target"),
            check("must_be_absent", path="a/target"),
        ]))
        self.put(self.workspace, "z/target", b"z")
        self.put(self.workspace, "a/target", b"a")
        first = self.invoke()
        result = self.result(first)
        self.assertEqual([(f["target"], f["mode"]) for f in result["findings"]], [
            ("a/target", "must_be_absent"), ("z/target", "must_be_absent")
        ])
        self.assertNotIn("id", result["findings"][0])
        self.assertEqual(first.stdout, self.invoke().stdout)

    def test_29_numeric_version_is_exact_integer(self) -> None:
        for version in (3.0, "3", True):
            with self.subTest(version=version):
                self.configure({"version": version, "checks": [check("must_be_absent", path="x")]})
                self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_30_runtime_imports_are_stdlib_or_relative(self) -> None:
        imported: set[str] = set()
        paths = sorted((ROOT / "workflow_template_drift").glob("*.py"))
        self.assertEqual(len(paths), 3)
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        self.assertEqual(imported - sys.stdlib_module_names, set())
        self.assertGreater(len(imported), 0)

    def test_31_head_source_and_target_too_short(self) -> None:
        self.configure(config([check("head_lines_equal", source="s/file", target="t/file", head_lines=2)]))
        self.put(self.source, "s/file", b"only one\n")
        self.put(self.workspace, "t/file", b"only one\n")
        completed = self.invoke()
        self.assertEqual((completed.returncode, self.result(completed)["error"]["code"]), (2, "invalid_source"))
        self.put(self.source, "s/file", b"one\ntwo\n")
        self.put(self.workspace, "t/file", b"one\n")
        completed = self.invoke()
        self.assertEqual((completed.returncode, self.result(completed)["findings"][0]["kind"]), (1, "target_too_short"))

    def test_32_escaped_nul_and_lone_surrogate_config(self) -> None:
        for raw in (
            b'{"version":3,"checks":[{"mode":"must_be_absent","path":"\\u0000"}]}\n',
            b'{"version":3,"checks":[{"mode":"must_be_absent","path":"\\ud800"}]}\n',
        ):
            with self.subTest(raw=raw):
                self.configure(raw, raw=True)
                self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_33_version_selection_and_nonempty_checks(self) -> None:
        for value in (
            {"version": 1, "files": []},
            {"version": 2, "byte_identical": [], "scaffold_starter": []},
            {"checks": [check("must_be_absent", path="x")]},
            {"version": 3, "checks": []},
        ):
            with self.subTest(value=value):
                self.configure(value)
                self.assertEqual(self.result()["error"]["code"], "invalid_config")

    def test_34_all_authoring_shapes_and_severity_default(self) -> None:
        values = [
            check("bytes_equal", path="same/bytes"),
            check("bytes_equal", source="s/bytes", target="t/bytes", severity="warning"),
            check("head_lines_equal", path="same/head", head_lines=1),
            check("head_lines_equal", source="s/head", target="t/head", head_lines=1),
            check("must_exist", path="t/exists"),
            check("must_exist", source="s/create", target="t/create"),
            check("must_be_absent", path="t/absent"),
        ]
        parsed = engine._schema_config(config(values))
        self.assertEqual(len(parsed), 7)
        self.assertEqual(parsed[0].source, parsed[0].target)
        self.assertIsNone(parsed[4].source)
        self.assertEqual(parsed[0].severity, "error")
        self.assertEqual(parsed[1].severity, "warning")

    def test_35_must_exist_source_is_read_only_when_target_missing(self) -> None:
        self.configure(config([check("must_exist", source="s/create", target="t/item")]))
        self.put(self.source, "s/create", b"created\n")
        self.put(self.workspace, "t/item", b"present\n")
        labels: list[str] = []
        original = engine._read_stable

        def observed(obj, path, label, budget):
            labels.append(label)
            return original(obj, path, label, budget)

        with mock.patch.object(engine, "_read_stable", side_effect=observed):
            result, exit_code = engine.evaluate(str(self.workspace), str(self.source), "drift.json", "error")
        self.assertEqual((exit_code, result["status"], labels.count("source")), (0, "PASS", 0))
        (self.workspace / "t/item").unlink()
        labels.clear()
        with mock.patch.object(engine, "_read_stable", side_effect=observed):
            result, exit_code = engine.evaluate(str(self.workspace), str(self.source), "drift.json", "error")
        self.assertEqual((exit_code, result["findings"][0]["fixable"], labels.count("source")), (1, True, 1))

    def test_36_result_union_is_closed_and_has_canonical_key_order(self) -> None:
        self.configure(config([check("must_exist", path="missing")]))
        completed = self.invoke()
        result = self.result(completed)
        self.assertEqual(list(result), ["version", "status", "findings", "patch"])
        self.assertEqual(list(result["findings"][0]), ["target", "mode", "severity", "kind", "fixable", "details"])
        self.assertNotIn(b'"message"', completed.stdout)
        self.configure({"version": 3, "checks": []})
        error_completed = self.invoke()
        error = self.result(error_completed)
        self.assertEqual(list(error), ["version", "status", "error"])
        self.assertEqual(list(error["error"])[:2], ["code", "message"])

    def test_37_same_size_in_place_rewrite_is_detected(self) -> None:
        self.configure(config([check("bytes_equal", source="s/file", target="t/file")]))
        source_path = self.put(self.source, "s/file", b"abcdefgh")
        self.put(self.workspace, "t/file", b"abcdefgh")
        write_fd = os.open(source_path, os.O_WRONLY)
        changed = False
        original = engine._read_stable

        def racing_read(obj, path, label, budget):
            nonlocal changed
            if not changed and label == "source" and path == "s/file":
                before = os.stat(source_path)
                os.pwrite(write_fd, b"Z", 0)
                os.fsync(write_fd)
                os.utime(source_path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
                changed = True
            return original(obj, path, label, budget)

        try:
            with mock.patch.object(engine, "_read_stable", side_effect=racing_read):
                result, exit_code = engine.evaluate(str(self.workspace), str(self.source), "drift.json", "error")
        finally:
            os.close(write_fd)
        self.assertTrue(changed)
        self.assertEqual((exit_code, result["status"], result["error"]["code"]), (2, "ERROR", "io_error"))
        self.assertEqual(result["error"]["message"], "source changed while being read")
        (ARTIFACTS / "snapshot-race.json").write_text(
            json.dumps({
                "same_size": os.stat(source_path).st_size == 8,
                "mutation_injected": changed,
                "mutation_phase": "after open snapshot, before source read",
                "exit": exit_code,
                "code": result["error"]["code"],
                "message": result["error"]["message"],
            }, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )

    @unittest.skipUnless(
        (ROOT / ".runtime").exists(),
        ".runtime is required for the isolated-layout host proof",
    )
    def test_38_no_pip_isolated_runtime_layout(self) -> None:
        site = ROOT / ".runtime" / "lib" / "python3.12" / "site-packages"
        entries = sorted(path.name for path in site.iterdir())
        self.assertEqual(entries, ["workflow_template_drift"])
        self.assertTrue((site / "workflow_template_drift").is_symlink())
        completed = subprocess.run(
            ["python3.12", "-I", "-X", "utf8", "-B", "-c", "import workflow_template_drift; print(workflow_template_drift.__file__)"],
            cwd=ROOT,
            env=runtime_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual((completed.returncode, completed.stderr), (0, b""))
        self.assertEqual(
            completed.stdout,
            (str(site / "workflow_template_drift" / "__init__.py") + "\n").encode(),
        )


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
