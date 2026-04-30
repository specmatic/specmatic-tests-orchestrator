from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OrchestratorTesterFixtureTest(unittest.TestCase):
    def test_orchestrator_tester_manifest_is_a_test_fixture(self) -> None:
        fixture = ROOT / "tests" / "resources" / "orchestrator-tester-test-executor.json"
        old_runtime_path = ROOT / "resources" / "orchestrator-tester-test-executor.json"

        self.assertTrue(fixture.exists())
        self.assertFalse(old_runtime_path.exists())

        manifest = json.loads(fixture.read_text(encoding="utf-8"))

        self.assertIsInstance(manifest, list)
        self.assertTrue(manifest)
        self.assertTrue(all("result" in item for item in manifest))


if __name__ == "__main__":
    unittest.main()
