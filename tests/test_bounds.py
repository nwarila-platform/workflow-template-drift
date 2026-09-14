from __future__ import annotations

import json
from pathlib import Path

from workflow_template_drift import engine

from .support import ARTIFACTS, CliCase, check, config


class BoundTests(CliCase):
    observations: dict[str, dict[str, object]] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "bounds.json").write_text(
            json.dumps(cls.observations, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )

    def observe(self, name: str, completed) -> dict[str, object]:
        result = self.result(completed)
        self.observations[name] = {
            "exit": completed.returncode,
            "code": result.get("error", {}).get("code"),
            "status": result["status"],
            "stdout_bytes": len(completed.stdout),
            "stderr_bytes": len(completed.stderr),
        }
        return result

    def sparse(self, root: Path, relative: str, size: int) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            stream.truncate(size)

    def test_config_file_byte_limit_pair(self) -> None:
        base = (json.dumps(config([check("must_be_absent", path="absent")]), separators=(",", ":")) + "\n").encode()
        pass_raw = base + b" " * (engine.MAX_CONFIG_BYTES - len(base))
        self.configure(pass_raw, raw=True)
        passed = self.invoke()
        self.assertEqual((passed.returncode, self.observe("config_bytes_at_limit", passed)["status"]), (0, "PASS"))
        self.configure(pass_raw + b" ", raw=True)
        failed = self.invoke()
        self.assertEqual((failed.returncode, self.observe("config_bytes_over_limit", failed)["error"]["code"]), (2, "resource_limit"))

    def test_json_nesting_depth_limit_pair(self) -> None:
        self.configure(b"[" * engine.MAX_JSON_DEPTH + b"]" * engine.MAX_JSON_DEPTH + b"\n", raw=True)
        passed_bound = self.invoke()
        result = self.observe("json_depth_at_limit", passed_bound)
        self.assertEqual((passed_bound.returncode, result["error"]["code"]), (2, "invalid_config"))
        self.configure(b"[" * (engine.MAX_JSON_DEPTH + 1) + b"]" * (engine.MAX_JSON_DEPTH + 1) + b"\n", raw=True)
        failed = self.invoke()
        result = self.observe("json_depth_over_limit", failed)
        self.assertEqual((failed.returncode, result["error"]["code"]), (2, "resource_limit"))

    def test_check_count_limit_pair(self) -> None:
        at_limit = [check("must_be_absent", path=f"p/{index:04d}") for index in range(engine.MAX_CHECKS)]
        self.configure(config(at_limit))
        passed = self.invoke()
        self.assertEqual((passed.returncode, self.observe("checks_at_limit", passed)["status"]), (0, "PASS"))
        at_limit.append(check("must_be_absent", path="p/over"))
        self.configure(config(at_limit))
        failed = self.invoke()
        self.assertEqual((failed.returncode, self.observe("checks_over_limit", failed)["error"]["code"]), (2, "resource_limit"))

    def test_source_file_byte_limit_pair(self) -> None:
        self.configure(config([check("bytes_equal", source="s/file", target="t/file")]))
        self.sparse(self.source, "s/file", engine.MAX_FILE_BYTES)
        self.sparse(self.workspace, "t/file", engine.MAX_FILE_BYTES)
        passed = self.invoke()
        self.assertEqual((passed.returncode, self.observe("source_bytes_at_limit", passed)["status"]), (0, "PASS"))
        self.sparse(self.source, "s/file", engine.MAX_FILE_BYTES + 1)
        failed = self.invoke()
        self.assertEqual((failed.returncode, self.observe("source_bytes_over_limit", failed)["error"]["code"]), (2, "resource_limit"))

    def test_target_file_byte_limit_pair(self) -> None:
        self.configure(config([check("must_exist", path="t/file")]))
        self.sparse(self.workspace, "t/file", engine.MAX_FILE_BYTES)
        passed = self.invoke()
        self.assertEqual((passed.returncode, self.observe("target_bytes_at_limit", passed)["status"]), (0, "PASS"))
        self.sparse(self.workspace, "t/file", engine.MAX_FILE_BYTES + 1)
        failed = self.invoke()
        self.assertEqual((failed.returncode, self.observe("target_bytes_over_limit", failed)["error"]["code"]), (2, "resource_limit"))

    def aggregate_fixture(self, extra_byte: bool) -> tuple[int, int]:
        target_count = 8 if extra_byte else 7
        checks = [check("bytes_equal", source="s/file", target=f"t/{index}") for index in range(target_count)]
        raw = (json.dumps(config(checks), separators=(",", ":")) + "\n").encode()
        self.configure(raw, raw=True)
        self.sparse(self.source, "s/file", engine.MAX_FILE_BYTES)
        for index in range(6):
            self.sparse(self.workspace, f"t/{index}", engine.MAX_FILE_BYTES)
        tail = engine.MAX_TOTAL_BYTES - len(raw) - 7 * engine.MAX_FILE_BYTES
        self.assertGreaterEqual(tail, 0)
        self.sparse(self.workspace, "t/6", tail)
        if extra_byte:
            self.sparse(self.workspace, "t/7", 1)
        return len(raw), tail

    def test_total_bytes_read_limit_pair(self) -> None:
        config_bytes, tail = self.aggregate_fixture(False)
        passed = self.invoke()
        result = self.observe("total_bytes_at_limit", passed)
        self.assertEqual((passed.returncode, result["status"]), (1, "FAIL"))
        self.observations["total_bytes_at_limit"]["configured_read_bytes"] = engine.MAX_TOTAL_BYTES
        self.observations["total_bytes_at_limit"]["config_bytes"] = config_bytes
        self.observations["total_bytes_at_limit"]["tail_target_bytes"] = tail

        config_bytes, tail = self.aggregate_fixture(True)
        failed = self.invoke()
        result = self.observe("total_bytes_over_limit", failed)
        self.assertEqual((failed.returncode, result["error"]["code"]), (2, "resource_limit"))
        self.observations["total_bytes_over_limit"]["bytes_before_offending_read"] = engine.MAX_TOTAL_BYTES
        self.observations["total_bytes_over_limit"]["offending_file_bytes"] = 1
        self.observations["total_bytes_over_limit"]["config_bytes"] = config_bytes
        self.observations["total_bytes_over_limit"]["tail_target_bytes"] = tail

    def test_head_lines_limit_pair(self) -> None:
        self.configure(config([check("head_lines_equal", path="same/head", head_lines=engine.MAX_HEAD_LINES)]))
        data = b"x\n" * engine.MAX_HEAD_LINES
        self.put(self.source, "same/head", data)
        self.put(self.workspace, "same/head", data)
        passed = self.invoke()
        self.assertEqual((passed.returncode, self.observe("head_lines_at_limit", passed)["status"]), (0, "PASS"))
        self.configure(config([check("head_lines_equal", path="same/head", head_lines=engine.MAX_HEAD_LINES + 1)]))
        failed = self.invoke()
        self.assertEqual((failed.returncode, self.observe("head_lines_over_limit", failed)["error"]["code"]), (2, "resource_limit"))


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
