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
            config_path = root / "test-executor.json"
            outputs_dir = root / "outputs"
            consolidated_dir = root / "consolidated_output"

            config_path.write_text(
                json.dumps(
                    [
                        {
                            "type": "sample-project",
                            "name": "contract-tests",
                            "description": "Contract checks",
                            "result": {
                                "kind": "happy-path",
                                "passed": True,
                                "total": 12,
                                "passed_count": 12,
                                "failed_count": 0,
                            },
                        },
                        {
                            "type": "playwright",
                            "name": "ui-tests",
                            "description": "UI checks",
                            "result": {
                                "kind": "smoke-failure",
                                "passed": False,
                                "total": 4,
                                "passed_count": 3,
                                "failed_count": 1,
                            },
                        },
                    ]
                ),
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
            ui_result = json.loads((outputs_dir / "playwright-ui-tests" / "result.json").read_text(encoding="utf-8"))

            self.assertEqual(contract_result["result_kind"], "happy-path")
            self.assertTrue(contract_result["passed"])
            self.assertEqual(ui_result["result_kind"], "smoke-failure")
            self.assertFalse(ui_result["passed"])
            self.assertEqual(summary["conclusion"], "failure")
            self.assertEqual(summary["failed_sources"], 1)


if __name__ == "__main__":
    unittest.main()
