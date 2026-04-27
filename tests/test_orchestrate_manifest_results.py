from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts import orchestrate
from scripts.consolidate_outputs import write_summary


class OrchestrateManifestResultsTest(unittest.TestCase):
    def test_manifest_result_blocks_control_output_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_root = Path(__file__).resolve().parents[1]
            config_path = root / "test-executor.json"
            outputs_dir = root / "outputs"
            consolidated_dir = root / "consolidated_output"

            config_path.write_text(
                repo_root.joinpath("tests", "resources", "test-executor-failure.json")
                .read_text(encoding="utf-8"),
                encoding="utf-8",
            )

            orchestrate.create_demo_source_results(
                outputs_dir=outputs_dir,
                jar_url="http://example.local/orchestrator-tester.jar",
                jar_path="/tmp/orchestrator-tester.jar",
                config_path=config_path,
            )
            summary = write_summary(outputs_dir, consolidated_dir)

            contract_result = json.loads((outputs_dir / "sample-project-contract-tests" / "result.json").read_text(encoding="utf-8"))
            asyncapi_result = json.loads((outputs_dir / "sample-project-asyncapi-tests" / "result.json").read_text(encoding="utf-8"))

            self.assertEqual(contract_result["result_kind"], "happy-path")
            self.assertTrue(contract_result["passed"])
            self.assertEqual(asyncapi_result["result_kind"], "mixed")
            self.assertFalse(asyncapi_result["passed"])
            self.assertEqual(summary["conclusion"], "failure")
            self.assertEqual(summary["failed_sources"], 1)


if __name__ == "__main__":
    unittest.main()
