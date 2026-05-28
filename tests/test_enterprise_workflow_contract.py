from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "specmatic-enterprise-jar-tests.yml"


class EnterpriseWorkflowContractTest(unittest.TestCase):
    def workflow_text(self) -> str:
        return WORKFLOW.read_text(encoding="utf-8")

    def test_repository_dispatch_uses_nested_enterprise_configuration_for_runner(self) -> None:
        text = self.workflow_text()

        expected_runner_expression = (
            "github.event.client_payload.enterprise_options.configuration || "
            "github.event.client_payload.enterprise_configuration || "
            "inputs.enterprise_configuration || 'ubuntu-latest'"
        )
        self.assertIn("name: Specmatic Tests Orchestrator", text)
        self.assertIn("github.event_name == 'repository_dispatch'", text)
        self.assertIn("Specmatic Tests Orchestrator - {0} {1} {2}", text)
        self.assertIn("github.event.client_payload.enterprise_options.repository_name", text)
        self.assertIn("github.event.client_payload.enterprise_options.run_number", text)
        self.assertIn("format('#{0}'", text)
        self.assertIn("github.event_name == 'push' && github.event.head_commit.message", text)
        self.assertIn("push:", text)
        self.assertIn("branches:", text)
        self.assertIn("run-orchestrator:", text)
        self.assertNotIn("validate-orchestrator:", text)
        self.assertIn("if: github.event_name == 'push'", text)
        self.assertIn("python -B -m unittest discover -s tests", text)
        self.assertIn("if: github.event_name != 'push'", text)
        self.assertIn(
            f"runs-on: ${{{{ github.event_name == 'push' && 'ubuntu-latest' || {expected_runner_expression} }}}}",
            text,
        )

    def test_repository_dispatch_accepts_grouped_payload_options(self) -> None:
        text = self.workflow_text()

        self.assertIn('options = payload.get("orchestrator_options")', text)
        self.assertIn('enterprise_options = payload.get("enterprise_options")', text)
        self.assertIn("def pick_enterprise(*names: str, default: str = \"\") -> str:", text)
        self.assertIn(
            '"ENTERPRISE_REPOSITORY": pick_enterprise("repository", "enterprise_repository", '
            'default="specmatic/enterprise")',
            text,
        )
        self.assertIn(
            '"ENTERPRISE_CONFIGURATION": pick_enterprise("configuration", "enterprise_configuration", default="ubuntu-latest")',
            text,
        )
        self.assertIn('test_executor_json = pick_option("test_executor_json")', text)
        self.assertIn('orchestrator_options.test_executor_json is required for caller-triggered runs', text)
        self.assertIn('values["ORCHESTRATOR_TEST_EXECUTOR_PATH"] = str(test_executor_path)', text)
        self.assertNotIn("run_parallel", text)
        self.assertNotIn("RUN_PARALLEL", text)
        self.assertNotIn("test_executor_path:", text)
        self.assertNotIn('pick_option("test_executor_path")', text)
