from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "specmatic-enterprise-jar-tests.yml"


class EnterpriseWorkflowContractTest(unittest.TestCase):
    def workflow_text(self) -> str:
        return WORKFLOW.read_text(encoding="utf-8")

    def test_repository_dispatch_uses_nested_enterprise_configuration_for_runner(self) -> None:
        text = self.workflow_text()

        expected_expression = (
            "github.event.client_payload.enterprise_options.configuration || "
            "github.event.client_payload.enterprise_configuration || "
            "inputs.enterprise_configuration || 'ubuntu-latest'"
        )
        self.assertIn(f"run-name: Specmatic Enterprise Jar Tests (${{{{ {expected_expression} }}}})", text)
        self.assertIn(f"runs-on: ${{{{ {expected_expression} }}}}", text)

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
        self.assertIn('"RUN_PARALLEL": pick_option("run_parallel", default="false").lower()', text)
