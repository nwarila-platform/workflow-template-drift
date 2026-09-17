from pathlib import Path
import unittest
from .support import ROOT, invoke

class DeterminismGoldenTests(unittest.TestCase):
    def test_text_and_patch_goldens(self) -> None:
        workspace = ROOT / "fixtures/determinism/workspace"
        template = ROOT / "fixtures/determinism/source"
        for output in ("text", "patch"):
            with self.subTest(output=output):
                completed = invoke(workspace, template, config_path="drift.json", output=output,
                                   labels=["example/determinism"])
                golden = (ROOT / "fixtures/goldens" / f"determinism.{output}").read_bytes()
                self.assertEqual((completed.returncode, completed.stderr, completed.stdout), (1, b"", golden))

if __name__ == "__main__":
    unittest.main(verbosity=2)
