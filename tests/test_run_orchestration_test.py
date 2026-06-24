from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import uuid
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run-orchestration-test.py"
SPEC = importlib.util.spec_from_file_location("run_orchestration_test", SCRIPT_PATH)
assert SPEC is not None
run_orchestration_test = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = run_orchestration_test
SPEC.loader.exec_module(run_orchestration_test)


@contextmanager
def workspace_temp_dir():
    path = Path.cwd() / "temp" / "unit-tests" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class RunOrchestrationTest(unittest.TestCase):
    def test_parse_env_line_handles_export_and_quotes(self) -> None:
        parsed = run_orchestration_test.parse_env_line('export ENTERPRISE_VERSION="3.0.0-SNAPSHOT"')
        self.assertEqual(parsed, ("ENTERPRISE_VERSION", "3.0.0-SNAPSHOT"))

    def test_load_default_env_files_loads_without_overriding_existing_env(self) -> None:
        with workspace_temp_dir() as temp_dir:
            (temp_dir / ".env").write_text("ENTERPRISE_VERSION=3.0.0-SNAPSHOT\n", encoding="utf-8")
            env_dir = temp_dir / "env"
            env_dir.mkdir(parents=True, exist_ok=True)
            (env_dir / ".env.local").write_text("ENTERPRISE_DOCKER_IMAGE=specmatic/enterprise:3.0.0-SNAPSHOT\n", encoding="utf-8")

            original_cwd = Path.cwd()
            original_env = run_orchestration_test.os.environ.copy()
            try:
                os.chdir(temp_dir)
                run_orchestration_test.os.environ["ENTERPRISE_VERSION"] = "already-set"
                run_orchestration_test.load_default_env_files()
            finally:
                os.chdir(original_cwd)

            try:
                self.assertEqual(run_orchestration_test.os.environ.get("ENTERPRISE_VERSION"), "already-set")
                self.assertEqual(
                    run_orchestration_test.os.environ.get("ENTERPRISE_DOCKER_IMAGE"),
                    "specmatic/enterprise:3.0.0-SNAPSHOT",
                )
            finally:
                run_orchestration_test.os.environ.clear()
                run_orchestration_test.os.environ.update(original_env)

    def test_clean_temp_dir_removes_stale_contents_and_recreates_directory(self) -> None:
        with workspace_temp_dir() as temp_dir:
            temp_root = temp_dir / "orchestration-temp"
            stale_file = temp_root / "sample-project" / "repo" / "stale.txt"
            stale_file.parent.mkdir(parents=True)
            stale_file.write_text("old checkout", encoding="utf-8")

            run_orchestration_test.clean_temp_dir(temp_root)

            self.assertTrue(temp_root.is_dir())
            self.assertFalse(stale_file.exists())

    def test_clean_temp_dir_refuses_current_working_directory(self) -> None:
        with self.assertRaises(ValueError):
            run_orchestration_test.clean_temp_dir(Path.cwd())

    def test_clean_outputs_dir_removes_stale_reports_and_recreates_directory(self) -> None:
        with workspace_temp_dir() as temp_dir:
            outputs_root = temp_dir / "outputs"
            stale_report = outputs_root / "sample-project" / "repo" / "gradle" / "index.html"
            stale_report.parent.mkdir(parents=True)
            stale_report.write_text("old report", encoding="utf-8")

            run_orchestration_test.clean_outputs_dir(outputs_root)

            self.assertTrue(outputs_root.is_dir())
            self.assertFalse(stale_report.exists())

    def test_clean_outputs_dir_refuses_current_working_directory(self) -> None:
        with self.assertRaises(ValueError):
            run_orchestration_test.clean_outputs_dir(Path.cwd())

    def test_github_repo_slug_extracts_owner_and_repo(self) -> None:
        self.assertEqual(
            run_orchestration_test.github_repo_slug("https://github.com/specmatic/specmatic-order-bff-java.git"),
            "specmatic/specmatic-order-bff-java",
        )

    def test_workflow_dispatch_inputs_only_include_declared_inputs(self) -> None:
        original_env = run_orchestration_test.os.environ.copy()
        try:
            run_orchestration_test.os.environ["GITHUB_RUN_NUMBER"] = "150"
            inputs = run_orchestration_test.workflow_dispatch_inputs_for(
                available_inputs={
                    "enterprise_version",
                    "specmatic_jar_url",
                    "enterprise_artifact_url",
                    "enterprise_jar_url",
                    "jar_url",
                    "orchestrator_run_suffix",
                },
                specmatic_version="",
                enterprise_version="1.2.3-SNAPSHOT",
                enterprise_docker_image="specmatic/studio:test",
                jar_url="https://example.com/specmatic.jar",
                jar_path="/tmp/specmatic.jar",
            )
        finally:
            run_orchestration_test.os.environ.clear()
            run_orchestration_test.os.environ.update(original_env)

        self.assertEqual(
            inputs,
            {
                "enterprise_version": "1.2.3-SNAPSHOT",
                "specmatic_jar_url": "https://example.com/specmatic.jar",
                "enterprise_artifact_url": "https://example.com/specmatic.jar",
                "enterprise_jar_url": "https://example.com/specmatic.jar",
                "jar_url": "https://example.com/specmatic.jar",
                "orchestrator_run_suffix": "Orchestrator #150",
            },
        )

    def test_workflow_dispatch_inputs_uses_suffix_override_when_present(self) -> None:
        inputs = run_orchestration_test.workflow_dispatch_inputs_for(
            available_inputs={"orchestrator_run_suffix"},
            specmatic_version="",
            enterprise_version="",
            enterprise_docker_image="",
            jar_url="",
            jar_path="",
            orchestrator_run_suffix_override="Orchestrator #150 retry 1",
        )

        self.assertEqual(inputs, {"orchestrator_run_suffix": "Orchestrator #150 retry 1"})

    def test_extract_workflow_dispatch_inputs(self) -> None:
        with workspace_temp_dir() as temp_dir:
            workflow = temp_dir / "workflow.yml"
            workflow.write_text(
                """
name: sample
on:
  workflow_dispatch:
    inputs:
      enterprise_version:
        type: string
      run_visual:
        type: boolean
""",
                encoding="utf-8",
            )

            self.assertEqual(
                run_orchestration_test.extract_workflow_dispatch_inputs(workflow),
                {"enterprise_version", "run_visual"},
            )

    def test_has_workflow_dispatch_trigger(self) -> None:
        with workspace_temp_dir() as temp_dir:
            dispatchable = temp_dir / "dispatchable.yml"
            dispatchable.write_text(
                """
name: sample
on:
  workflow_dispatch:
  push:
""",
                encoding="utf-8",
            )
            push_only = temp_dir / "push-only.yml"
            push_only.write_text(
                """
name: sample
on:
  push:
""",
                encoding="utf-8",
            )
            inline = temp_dir / "inline.yml"
            inline.write_text("name: sample\non: [push, workflow_dispatch]\n", encoding="utf-8")

            self.assertTrue(run_orchestration_test.has_workflow_dispatch_trigger(dispatchable))
            self.assertTrue(run_orchestration_test.has_workflow_dispatch_trigger(inline))
            self.assertFalse(run_orchestration_test.has_workflow_dispatch_trigger(push_only))

    def test_non_dispatchable_workflow_is_reported_as_skipped_without_failing_summary(self) -> None:
        result = run_orchestration_test.WorkflowResult(
            type="sample-project",
            repository="contract-tests",
            repo_url="https://github.com/specmatic/specmatic-order-bff-java.git",
            branch="main",
            workflow=".github/workflows/gradle.yml",
            status=run_orchestration_test.STATUS_SKIPPED,
            exit_code=0,
            duration_seconds=0,
            commands=[],
            executed_commands=[],
            output_dir="outputs/sample-project/contract-tests/gradle",
            log_file="outputs/sample-project/contract-tests/gradle/run.log",
            copied_result_paths=[],
            total_tests=0,
            failed_tests=0,
            skipped_tests=0,
            started_at="2026-04-22T05:00:00+00:00",
            finished_at="2026-04-22T05:00:00+00:00",
            details=".github/workflows/gradle.yml cannot be dispatched because it does not declare workflow_dispatch.",
        )

        summary = run_orchestration_test.build_summary([result])

        self.assertEqual(summary["conclusion"], "success")
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["error_summary"], [])
        self.assertEqual(len(summary["non_dispatchable_workflows"]), 1)
        main_table = run_orchestration_test.render_summary_table(
            run_orchestration_test.dispatchable_results([result])
        )
        skipped_table = run_orchestration_test.render_non_dispatchable_workflow_table([result])

        self.assertNotIn("sample-project/contract-tests", main_table)
        self.assertIn("sample-project/contract-tests", skipped_table)
        self.assertIn("missing workflow_dispatch", skipped_table)

    def test_discover_remote_workflow_files_reads_workflows_via_github_api(self) -> None:
        executor = run_orchestration_test.TestExecutor(
            type="sample-project",
            github_url="https://github.com/specmatic/example.git",
            name="example",
            branch="main",
            description="",
            workflow_globs=[".github/workflows/*.yml"],
            workflow_files=[],
            command=[],
            result_paths=[],
        )

        def fake_github_api_json(method: str, url: str, token: str, payload=None, ok_statuses=None):
            if url.endswith("/contents/.github/workflows?ref=main"):
                return [
                    {"type": "file", "path": ".github/workflows/gradle.yml"},
                    {"type": "file", "path": ".github/workflows/readme.txt"},
                ]
            if url.endswith("/contents/.github/workflows/gradle.yml?ref=main"):
                return {
                    "type": "file",
                    "path": ".github/workflows/gradle.yml",
                    "encoding": "base64",
                    "content": "bmFtZTogZ3JhZGxlCm9uOgogIHdvcmtmbG93X2Rpc3BhdGNoOgo=",
                }
            raise AssertionError(f"Unexpected GitHub API url: {url}")

        with mock.patch.object(run_orchestration_test, "github_api_json", side_effect=fake_github_api_json):
            workflows = run_orchestration_test.discover_remote_workflow_files(
                repo_slug="specmatic/example",
                ref="main",
                executor=executor,
                token="token",
                api_base_url="https://api.github.com",
            )

        self.assertEqual(len(workflows), 1)
        self.assertEqual(workflows[0].label, ".github/workflows/gradle.yml")
        self.assertIn("workflow_dispatch", workflows[0].text)

    def test_build_summary_treats_skipped_results_as_successful(self) -> None:
        result = run_orchestration_test.WorkflowResult(
            type="playwright-tests",
            repository="ui-tests",
            repo_url="https://example.com/repo.git",
            branch="main",
            workflow="_skipped",
            status=run_orchestration_test.STATUS_SKIPPED,
            exit_code=0,
            duration_seconds=0,
            commands=[],
            executed_commands=[],
            output_dir="outputs/playwright-tests/ui-tests/_skipped",
            log_file="outputs/playwright-tests/ui-tests/_skipped/run.log",
            copied_result_paths=[],
            total_tests=0,
            failed_tests=0,
            skipped_tests=0,
            started_at="2026-04-22T05:00:00+00:00",
            finished_at="2026-04-22T05:00:00+00:00",
            details="skipped Playwright executor on windows enterprise configuration",
        )

        summary = run_orchestration_test.build_summary([result])

        self.assertEqual(summary["conclusion"], "success")
        self.assertEqual(summary["failed_count"], 0)
        self.assertEqual(summary["passed_count"], 1)
        self.assertEqual(summary["error_summary"], [])

    def test_validate_required_enterprise_version_reports_missing_value(self) -> None:
        original_env = run_orchestration_test.os.environ.copy()
        try:
            run_orchestration_test.os.environ.pop("ENTERPRISE_VERSION", None)
            error = run_orchestration_test.validate_required_enterprise_version(
                mock.Mock(enterprise_version="")
            )
            self.assertIn("ENTERPRISE_VERSION is required", error)
        finally:
            run_orchestration_test.os.environ.clear()
            run_orchestration_test.os.environ.update(original_env)

    def test_validate_required_enterprise_version_accepts_env_value(self) -> None:
        original_env = run_orchestration_test.os.environ.copy()
        try:
            run_orchestration_test.os.environ["ENTERPRISE_VERSION"] = "1.2.3"
            error = run_orchestration_test.validate_required_enterprise_version(
                mock.Mock(enterprise_version="")
            )
            self.assertEqual(error, "")
        finally:
            run_orchestration_test.os.environ.clear()
            run_orchestration_test.os.environ.update(original_env)

    def test_validate_required_enterprise_version_accepts_cli_value(self) -> None:
        original_env = run_orchestration_test.os.environ.copy()
        try:
            run_orchestration_test.os.environ.pop("ENTERPRISE_VERSION", None)
            error = run_orchestration_test.validate_required_enterprise_version(
                mock.Mock(enterprise_version="1.2.3")
            )
            self.assertEqual(error, "")
        finally:
            run_orchestration_test.os.environ.clear()
            run_orchestration_test.os.environ.update(original_env)

    def test_validate_required_enterprise_version_accepts_repository_url(self) -> None:
        original_env = run_orchestration_test.os.environ.copy()
        try:
            run_orchestration_test.os.environ.pop("ENTERPRISE_VERSION", None)
            error = run_orchestration_test.validate_required_enterprise_version(
                mock.Mock(
                    enterprise_version=(
                        "https://repo.specmatic.io/#/snapshots/io/specmatic/"
                        "enterprise/executable-all/1.12.1-SNAPSHOT"
                    )
                )
            )
            self.assertEqual(error, "")
        finally:
            run_orchestration_test.os.environ.clear()
            run_orchestration_test.os.environ.update(original_env)

    def test_resolve_enterprise_version_snapshot_downloads_latest_timestamped_jar(self) -> None:
        metadata_by_url = {
            (
                "https://central.sonatype.com/repository/maven-snapshots/io/specmatic/enterprise/"
                "executable-all/1.12.1-SNAPSHOT/maven-metadata.xml"
            ): """
<metadata>
  <versioning>
    <snapshotVersions>
      <snapshotVersion>
        <extension>jar</extension>
        <value>1.12.1-20260427.120947-1</value>
      </snapshotVersion>
    </snapshotVersions>
  </versioning>
</metadata>
""",
        }
        original_read_remote_text = run_orchestration_test.read_remote_text
        try:
            run_orchestration_test.read_remote_text = lambda url: metadata_by_url[url]

            artifact = run_orchestration_test.resolve_enterprise_artifact_selector("1.12.1-SNAPSHOT")

            self.assertEqual(artifact.version, "1.12.1-SNAPSHOT")
            self.assertEqual(
                artifact.jar_url,
                (
                    "https://central.sonatype.com/repository/maven-snapshots/io/specmatic/enterprise/executable-all/"
                    "1.12.1-SNAPSHOT/executable-all-1.12.1-20260427.120947-1.jar"
                ),
            )
        finally:
            run_orchestration_test.read_remote_text = original_read_remote_text

    def test_resolve_enterprise_snapshot_repo_url_downloads_latest_version_jar(self) -> None:
        base_url = "https://repo.specmatic.io/snapshots/io/specmatic/enterprise/executable-all"
        metadata_by_url = {
            f"{base_url}/maven-metadata.xml": """
<metadata>
  <versioning>
    <latest>1.12.1-SNAPSHOT</latest>
  </versioning>
</metadata>
""",
            f"{base_url}/1.12.1-SNAPSHOT/maven-metadata.xml": """
<metadata>
  <versioning>
    <snapshotVersions>
      <snapshotVersion>
        <extension>jar</extension>
        <value>1.12.1-20260427.120947-1</value>
      </snapshotVersion>
    </snapshotVersions>
  </versioning>
</metadata>
""",
        }
        original_read_remote_text = run_orchestration_test.read_remote_text
        try:
            run_orchestration_test.read_remote_text = lambda url: metadata_by_url[url]

            artifact = run_orchestration_test.resolve_enterprise_artifact_selector(base_url)

            self.assertEqual(artifact.version, "1.12.1-SNAPSHOT")
            self.assertTrue(artifact.jar_url.endswith("/executable-all-1.12.1-20260427.120947-1.jar"))
        finally:
            run_orchestration_test.read_remote_text = original_read_remote_text

    def test_resolve_enterprise_direct_jar_url_uses_it_directly(self) -> None:
        jar_url = (
            "https://repo.specmatic.io/snapshots/io/specmatic/enterprise/executable-all/"
            "1.12.1-SNAPSHOT/executable-all-1.12.1-20260427.120947-1.jar"
        )

        artifact = run_orchestration_test.resolve_enterprise_artifact_selector(jar_url)

        self.assertEqual(artifact.version, "1.12.1-SNAPSHOT")
        self.assertEqual(artifact.jar_url, jar_url)

    def test_validate_required_enterprise_version_accepts_existing_executable_artifact_url(self) -> None:
        error = run_orchestration_test.validate_required_enterprise_version(
            mock.Mock(
                enterprise_version=(
                    "https://repo.specmatic.io/snapshots/io/specmatic/enterprise/executable/"
                    "1.12.1-SNAPSHOT/executable-1.12.1-20260427.120947-1.jar"
                )
            )
        )

        self.assertEqual(error, "")

    def test_resolve_enterprise_artifact_inputs_preserves_explicit_dummy_jar_url(self) -> None:
        artifact = run_orchestration_test.resolve_enterprise_artifact_inputs(
            "0.0.0-DUMMY",
            "https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar",
            "",
        )

        self.assertEqual(artifact.version, "0.0.0-DUMMY")
        self.assertEqual(
            artifact.jar_url,
            "https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar",
        )

    def test_resolve_enterprise_release_selector_downloads_latest_release_jar(self) -> None:
        base_url = "https://repo.specmatic.io/releases/io/specmatic/enterprise/executable-all"
        metadata_by_url = {
            f"{base_url}/maven-metadata.xml": """
<metadata>
  <versioning>
    <release>1.12.0</release>
  </versioning>
</metadata>
""",
        }
        original_read_remote_text = run_orchestration_test.read_remote_text
        try:
            run_orchestration_test.read_remote_text = lambda url: metadata_by_url[url]

            artifact = run_orchestration_test.resolve_enterprise_artifact_selector("RELEASE")

            self.assertEqual(artifact.version, "1.12.0")
            self.assertEqual(
                artifact.jar_url,
                "https://repo.specmatic.io/releases/io/specmatic/enterprise/executable-all/1.12.0/executable-all-1.12.0.jar",
            )
        finally:
            run_orchestration_test.read_remote_text = original_read_remote_text

    def test_main_logs_enterprise_artifact_resolution(self) -> None:
        with workspace_temp_dir() as temp_dir:
            config = temp_dir / "test-executor.json"
            config.write_text("[]", encoding="utf-8")
            original_argv = sys.argv[:]
            original_resolve = run_orchestration_test.resolve_enterprise_artifact_inputs
            try:
                sys.argv = [
                    "run-orchestration-test.py",
                    "--config",
                    str(config),
                    "--enterprise-version",
                    "SNAPSHOT",
                ]
                run_orchestration_test.resolve_enterprise_artifact_inputs = lambda enterprise_version, jar_url, jar_path: run_orchestration_test.EnterpriseArtifact(
                    version="1.12.1-SNAPSHOT",
                    jar_url=(
                        "https://repo.specmatic.io/snapshots/io/specmatic/enterprise/executable-all/"
                        "1.12.1-SNAPSHOT/executable-all-1.12.1-20260429.014552-3.jar"
                    ),
                )

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = run_orchestration_test.main()

                self.assertEqual(exit_code, 1)
                output = stdout.getvalue()
                self.assertIn("Test executor manifest:", output)
                self.assertIn(f"resolved path={config}", output)
                self.assertIn("Enterprise artifact resolution:", output)
                self.assertIn("requested ENTERPRISE_VERSION='SNAPSHOT'", output)
                self.assertIn("resolved enterprise_version='1.12.1-SNAPSHOT'", output)
                self.assertIn("resolved jar_url=https://repo.specmatic.io/", output)
            finally:
                sys.argv = original_argv
                run_orchestration_test.resolve_enterprise_artifact_inputs = original_resolve

    def test_run_executor_uses_synthetic_result_profile_without_repository(self) -> None:
        with workspace_temp_dir() as temp_dir:
            executor = run_orchestration_test.TestExecutor(
                type="sample-project",
                github_url="",
                name="contract-tests",
                branch="",
                description="",
                workflow_globs=[],
                workflow_files=[],
                command=[],
                result_paths=[],
                result_profile={
                    "kind": "happy-path",
                    "passed": True,
                    "total": 12,
                    "failed_count": 0,
                    "skipped_count": 1,
                },
            )

            results = run_orchestration_test.run_executor(
                executor=executor,
                outputs_dir=temp_dir / "outputs",
                github_token="token",
                api_base_url="https://api.github.com",
                poll_seconds=1,
                timeout_seconds=1,
                enterprise_version="0.0.0-DUMMY",
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].status, run_orchestration_test.STATUS_PASSED)
            self.assertEqual(results[0].total_tests, 12)
            self.assertEqual(results[0].skipped_tests, 1)
            self.assertEqual(results[0].workflow, "_profile")

    def test_extracts_test_commands_from_workflow(self) -> None:
        with workspace_temp_dir() as temp_dir:
            workflow = temp_dir / "workflow.yml"
            workflow.write_text(
                """
name: tests
jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - name: Run tests
        run: |
          npm test
          npm run report
""".lstrip(),
                encoding="utf-8",
            )

            commands = run_orchestration_test.extract_run_commands(workflow)

            self.assertEqual(commands, ["npm test"])

    def test_parses_reusable_workflow_call_inputs(self) -> None:
        lines = [
            "jobs:",
            "  execute-contract-tests:",
            "    uses: ./.github/workflows/playwright-test-group.yml",
            "    with:",
            "      test_path: specs/openapi/execute-contract-tests",
            "      artifact_name: execute-contract-tests",
            "      group_name: Contract",
        ]

        calls = run_orchestration_test.parse_reusable_workflow_calls(lines)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].workflow_path, ".github/workflows/playwright-test-group.yml")
        self.assertEqual(calls[0].inputs["test_path"], "specs/openapi/execute-contract-tests")
        self.assertEqual(calls[0].inputs["artifact_name"], "execute-contract-tests")
        self.assertEqual(calls[0].inputs["group_name"], "Contract")

    def test_extracts_playwright_commands_from_reusable_workflow(self) -> None:
        with workspace_temp_dir() as temp_dir:
            workflow_dir = temp_dir / ".github" / "workflows"
            workflow_dir.mkdir(parents=True, exist_ok=True)
            caller = workflow_dir / "playwright-openapi-contract.yml"
            reusable = workflow_dir / "playwright-test-group.yml"

            caller.write_text(
                """
name: Studio - OpenAPI Contract Tests
jobs:
  execute-contract-tests:
    uses: ./.github/workflows/playwright-test-group.yml
    with:
      test_path: specs/openapi/execute-contract-tests
      artifact_name: execute-contract-tests
      group_name: Contract
""".lstrip(),
                encoding="utf-8",
            )
            reusable.write_text(
                """
name: Playwright Test Group
on:
  workflow_call:
    inputs:
      test_path:
        required: true
        type: string
jobs:
  test:
    steps:
      - name: Install dependencies
        run: |
          npm ci
          npx playwright install chromium --with-deps
      - name: Run Playwright tests
        run: npx playwright test ${{ inputs.test_path }}
""".lstrip(),
                encoding="utf-8",
            )

            commands = run_orchestration_test.extract_workflow_commands(caller, temp_dir)

            command_values = [command.command for command in commands]
            self.assertIn("npm ci", command_values)
            self.assertIn("npx playwright install chromium --with-deps", command_values)
            self.assertIn("npx playwright test specs/openapi/execute-contract-tests", command_values)

    def test_skips_reusable_only_playwright_workflow_for_dispatch(self) -> None:
        workflow_text = """
name: Playwright Test Group
on:
  workflow_call:
    inputs:
      test_path:
        required: true
        type: string
jobs:
  test:
    steps:
      - run: npx playwright test ${{ inputs.test_path }}
""".lstrip()

        self.assertFalse(
            run_orchestration_test.should_consider_workflow_for_execution_text(
                workflow_text,
                ".github/workflows/playwright-test-group.yml",
            )
        )

    def test_skips_setup_only_workflow_even_when_dispatchable(self) -> None:
        workflow_text = """
name: Copilot Setup Steps
on:
  workflow_dispatch:
  push:
jobs:
  copilot-setup-steps:
    steps:
      - run: npm ci
      - run: npx playwright install --with-deps
      - run: npm run build
""".lstrip()

        self.assertFalse(
            run_orchestration_test.should_consider_workflow_for_execution_text(
                workflow_text,
                ".github/workflows/copilot-setup-steps.yml",
            )
        )

    def test_keeps_dispatchable_workflow_that_calls_reusable_test_workflow(self) -> None:
        workflow_text = """
name: Studio Contract Tests
on:
  workflow_dispatch:
jobs:
  execute-contract-tests:
    uses: ./.github/workflows/playwright-test-group.yml
    with:
      test_path: specs/openapi/execute-contract-tests
""".lstrip()

        self.assertTrue(
            run_orchestration_test.should_consider_workflow_for_execution_text(
                workflow_text,
                ".github/workflows/playwright-openapi-contract.yml",
            )
        )

    def test_keeps_dispatchable_matrix_workflow_with_expanded_test_command(self) -> None:
        workflow_text = """
name: Java CI with Gradle
on:
  workflow_dispatch:
jobs:
  buildAndTest:
    strategy:
      matrix:
        include:
          - name: docker
            testName: ContractTestsUsingTestContainer
            needsCliInstall: false
          - name: cli
            testName: ContractTestUsingCLITest
            needsCliInstall: true
    steps:
      - name: Run BFF Contract Test
        run: |
          ./gradlew test \
            --tests="com.component.orders.contract.${{ matrix.testName }}"
""".lstrip()

        self.assertTrue(
            run_orchestration_test.should_consider_workflow_for_execution_text(
                workflow_text,
                ".github/workflows/gradle.yml",
            )
        )

    def test_keeps_dispatchable_simple_matrix_workflow_with_matrix_expression_command(self) -> None:
        workflow_text = """
name: Java CI with Gradle
on:
  workflow_dispatch:
jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        receive: [kafka, sqs, mqtt, jms, amqp]
    steps:
      - name: Run tests
        shell: bash
        run: |
          set -euo pipefail
          ./gradlew test --tests "**.ContractTest" \
            -Dreceive.protocol=${{ matrix.receive }} \
            --no-daemon --rerun-tasks
""".lstrip()

        self.assertTrue(
            run_orchestration_test.should_consider_workflow_for_execution_text(
                workflow_text,
                ".github/workflows/gradle.yml",
            )
        )

    def test_expands_matrix_include_commands(self) -> None:
        original_is_linux_host = run_orchestration_test.is_linux_host
        try:
            run_orchestration_test.is_linux_host = lambda: True
            with workspace_temp_dir() as temp_dir:
                workflow = temp_dir / "workflow.yml"
                workflow.write_text(
                    """
name: tests
jobs:
  test:
    strategy:
      matrix:
        include:
          - name: docker
            testName: ContractTestsUsingTestContainer
            needsCliInstall: false
          - name: cli
            testName: ContractTestUsingCLITest
            needsCliInstall: true
          - name: junit
            testName: ContractTests
            needsCliInstall: false
    steps:
      - name: Matrix test
        run: ./gradlew test --tests="com.example.${{ matrix.testName }}"
""".lstrip(),
                    encoding="utf-8",
                )

                commands = run_orchestration_test.extract_run_commands(workflow)

                self.assertEqual(
                    commands,
                    [
                        './gradlew test --tests="com.example.ContractTestsUsingTestContainer"',
                        './gradlew test --tests="com.example.ContractTestUsingCLITest"',
                        './gradlew test --tests="com.example.ContractTests"',
                    ],
                )
        finally:
            run_orchestration_test.is_linux_host = original_is_linux_host

    def test_skips_matrix_rows_that_need_cli_install(self) -> None:
        original_is_linux_host = run_orchestration_test.is_linux_host
        try:
            run_orchestration_test.is_linux_host = lambda: True
            matrix = [
                {"name": "docker", "testName": "ContractTestsUsingTestContainer", "needsCliInstall": "false"},
                {"name": "cli", "testName": "ContractTestUsingCLITest", "needsCliInstall": "true"},
                {"name": "junit", "testName": "ContractTests", "needsCliInstall": "false"},
            ]

            expanded = run_orchestration_test.expand_matrix_expressions(
                './gradlew test --tests="com.example.${{ matrix.testName }}"',
                matrix,
            )
            commands = [command for command, _ in expanded]

            self.assertEqual(
                commands,
                [
                    './gradlew test --tests="com.example.ContractTestsUsingTestContainer"',
                    './gradlew test --tests="com.example.ContractTestUsingCLITest"',
                    './gradlew test --tests="com.example.ContractTests"',
                ],
            )
        finally:
            run_orchestration_test.is_linux_host = original_is_linux_host

    def test_skips_docker_matrix_row_on_non_linux_host(self) -> None:
        original_is_linux_host = run_orchestration_test.is_linux_host
        try:
            run_orchestration_test.is_linux_host = lambda: False
            matrix = [
                {"name": "docker", "testName": "ContractTestsUsingTestContainer", "needsCliInstall": "false"},
                {"name": "junit", "testName": "ContractTests", "needsCliInstall": "false"},
            ]

            expanded = run_orchestration_test.expand_matrix_expressions(
                './gradlew test --tests="com.example.${{ matrix.testName }}"',
                matrix,
            )
            commands = [command for command, _ in expanded]

            self.assertEqual(commands, ['./gradlew test --tests="com.example.ContractTests"'])
        finally:
            run_orchestration_test.is_linux_host = original_is_linux_host

    def test_skips_unresolved_github_expression_commands_without_matrix_values(self) -> None:
        with workspace_temp_dir() as temp_dir:
            workflow = temp_dir / "workflow.yml"
            workflow.write_text(
                """
name: tests
jobs:
  test:
    steps:
      - name: Matrix test
        run: ./gradlew test --tests="com.example.${{ matrix.testName }}"
""".lstrip(),
                encoding="utf-8",
            )

            commands = run_orchestration_test.extract_run_commands(workflow)

            self.assertEqual(commands, [])

    def test_skips_jacoco_report_commands_that_exclude_tests(self) -> None:
        self.assertFalse(run_orchestration_test.is_test_command("./gradlew jacocoTestReport -x test"))

    def test_keeps_gradle_test_commands_that_also_generate_jacoco_reports(self) -> None:
        self.assertTrue(
            run_orchestration_test.is_test_command(
                "./gradlew --no-daemon clean test bootJar jacocoTestReport"
            )
        )

    def test_treats_playwright_install_as_runnable_setup_command(self) -> None:
        self.assertTrue(
            run_orchestration_test.is_runnable_workflow_command(
                "npx playwright install chromium --with-deps",
                ".github/workflows/playwright-test-group.yml",
            )
        )
        self.assertTrue(
            run_orchestration_test.is_runnable_workflow_command(
                "npm ci",
                ".github/workflows/playwright-test-group.yml",
            )
        )
        self.assertFalse(
            run_orchestration_test.is_runnable_workflow_command(
                "npm ci",
                ".github/workflows/gradle.yml",
            )
        )

    def test_treats_playwright_group_wrapper_script_as_runnable_test_command(self) -> None:
        self.assertTrue(
            run_orchestration_test.is_runnable_workflow_command(
                "bash scripts/github/run-playwright-group.sh",
                ".github/workflows/playwright-matrix.yml",
            )
        )
        self.assertFalse(
            run_orchestration_test.is_runnable_workflow_command(
                "bash scripts/github/run-playwright-group.sh",
                ".github/workflows/gradle.yml",
            )
        )

    def test_keeps_dispatchable_playwright_workflow_when_test_logic_is_wrapped_in_script(self) -> None:
        workflow_text = """
name: Studio - Matrix Playwright Tests
on:
  workflow_dispatch:
jobs:
  matrix-tests:
    strategy:
      matrix:
        group:
          - workflow_name: OpenAPI Examples
            test_path: specs/openapi/generate-valid-examples
    steps:
      - uses: actions/checkout@v4
      - name: Install dependencies and set up environment
        run: bash scripts/github/setup-playwright-ci.sh
      - name: Run Playwright tests
        run: bash scripts/github/run-playwright-group.sh
      - name: Fail job if there are test failures
        run: bash scripts/github/verify-playwright-results.sh
""".lstrip()

        self.assertTrue(
            run_orchestration_test.should_consider_workflow_for_execution_text(
                workflow_text,
                ".github/workflows/playwright-matrix.yml",
            )
        )

    def test_apply_gradle_version_overrides_adds_properties(self) -> None:
        command = ["./gradlew", "test"]
        overridden = run_orchestration_test.apply_gradle_version_overrides(
            command,
            specmatic_version="2.0.0-SNAPSHOT",
            enterprise_version="3.0.0-SNAPSHOT",
        )
        self.assertIn("-PspecmaticVersion=2.0.0-SNAPSHOT", overridden)
        self.assertIn("-PspecmaticEnterpriseVersion=3.0.0-SNAPSHOT", overridden)
        self.assertIn("-PenterpriseVersion=3.0.0-SNAPSHOT", overridden)

    def test_apply_gradle_version_overrides_adds_snapshot_repo_url(self) -> None:
        command = ["./gradlew", "test"]
        overridden = run_orchestration_test.apply_gradle_version_overrides(
            command,
            specmatic_version="",
            enterprise_version="3.0.0-SNAPSHOT",
            snapshot_repo_url="file:///tmp/specmatic-maven",
        )

        self.assertIn("-PsnapshotRepoUrl=file:///tmp/specmatic-maven", overridden)

    def test_apply_gradle_version_overrides_skips_non_gradle_commands(self) -> None:
        command = ["npx", "playwright", "test", "specs/openapi"]
        overridden = run_orchestration_test.apply_gradle_version_overrides(
            command,
            specmatic_version="2.0.0-SNAPSHOT",
            enterprise_version="3.0.0-SNAPSHOT",
        )
        self.assertEqual(overridden, command)

    def test_select_runnable_commands_skips_setup_only_workflow(self) -> None:
        setup_only = [
            run_orchestration_test.WorkflowCommand(
                workflow_file=".github/workflows/copilot-setup-steps.yml",
                step_name="Install Playwright",
                command="npx playwright install --with-deps",
                working_directory=".",
                needs_cli_install=False,
            )
        ]
        selected = run_orchestration_test.select_runnable_commands(setup_only)
        self.assertEqual(selected, [])

    def test_select_runnable_commands_keeps_setup_when_test_exists(self) -> None:
        commands = [
            run_orchestration_test.WorkflowCommand(
                workflow_file=".github/workflows/playwright-test-group.yml",
                step_name="Install dependencies",
                command="npm ci",
                working_directory=".",
                needs_cli_install=False,
            ),
            run_orchestration_test.WorkflowCommand(
                workflow_file=".github/workflows/playwright-test-group.yml",
                step_name="Run tests",
                command="npx playwright test specs/openapi/execute-contract-tests",
                working_directory=".",
                needs_cli_install=False,
            ),
        ]
        selected = run_orchestration_test.select_runnable_commands(commands)
        self.assertEqual(selected, commands)

    def test_identifies_reusable_only_workflow(self) -> None:
        with workspace_temp_dir() as temp_dir:
            reusable = temp_dir / "playwright-test-group.yml"
            reusable.write_text(
                """
name: Playwright Test Group
on:
  workflow_call:
    inputs:
      test_path:
        required: true
        type: string
jobs: {}
""".lstrip(),
                encoding="utf-8",
            )
            self.assertTrue(run_orchestration_test.is_reusable_only_workflow(reusable))

    def test_does_not_mark_regular_workflow_as_reusable_only(self) -> None:
        with workspace_temp_dir() as temp_dir:
            regular = temp_dir / "playwright-openapi-contract.yml"
            regular.write_text(
                """
name: Studio - OpenAPI Contract Tests
on:
  push:
    branches: [main]
jobs: {}
""".lstrip(),
                encoding="utf-8",
            )
            self.assertFalse(run_orchestration_test.is_reusable_only_workflow(regular))

    def test_normalize_command_for_os_prefers_cmd_launcher_on_windows(self) -> None:
        with workspace_temp_dir() as temp_dir:
            with mock.patch.object(run_orchestration_test.os, "name", "nt"):
                with mock.patch.object(run_orchestration_test.shutil, "which", return_value=r"C:\Program Files\nodejs\npx.cmd"):
                    command = run_orchestration_test.normalize_command_for_os(
                        ["npx", "playwright", "install", "chromium", "--with-deps"],
                        temp_dir,
                    )
            self.assertEqual(command[0], r"C:\Program Files\nodejs\npx.cmd")
            self.assertNotIn("--with-deps", command)

    def test_identifies_playwright_executor(self) -> None:
        self.assertTrue(
            run_orchestration_test.is_playwright_executor(
                run_orchestration_test.TestExecutor(
                    type="playwright",
                    github_url="https://example.com/repo.git",
                    name="repo",
                    branch="main",
                    description="",
                    workflow_globs=[],
                    workflow_files=[],
                    command=[],
                    result_paths=[],
                )
            )
        )
        self.assertFalse(
            run_orchestration_test.is_playwright_executor(
                run_orchestration_test.TestExecutor(
                    type="sample-project",
                    github_url="https://example.com/repo.git",
                    name="repo",
                    branch="main",
                    description="",
                    workflow_globs=[],
                    workflow_files=[],
                    command=[],
                    result_paths=[],
                )
            )
        )
        self.assertTrue(
            run_orchestration_test.is_sample_project_executor(
                run_orchestration_test.TestExecutor(
                    type="sample-project",
                    github_url="https://example.com/repo.git",
                    name="repo",
                    branch="main",
                    description="",
                    workflow_globs=[],
                    workflow_files=[],
                    command=[],
                    result_paths=[],
                )
            )
        )

    def test_skips_playwright_executor_on_windows_enterprise_configuration(self) -> None:
        executor = run_orchestration_test.TestExecutor(
            type="playwright-tests",
            github_url="https://example.com/repo.git",
            name="ui-tests",
            branch="main",
            description="",
            workflow_globs=[],
            workflow_files=[],
            command=[],
            result_paths=[],
        )
        original_env = run_orchestration_test.os.environ.copy()
        try:
            run_orchestration_test.os.environ["ENTERPRISE_CONFIGURATION"] = "windows-latest"
            with workspace_temp_dir() as temp_dir:
                results = run_orchestration_test.run_executor(
                    executor=executor,
                    outputs_dir=temp_dir / "outputs",
                    github_token="token",
                    api_base_url="https://api.github.com",
                    poll_seconds=1,
                    timeout_seconds=1,
                )
        finally:
            run_orchestration_test.os.environ.clear()
            run_orchestration_test.os.environ.update(original_env)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, run_orchestration_test.STATUS_SKIPPED)
        self.assertEqual(results[0].exit_code, 0)
        self.assertIn("skipped Playwright executor on windows", results[0].details)

    def test_build_command_env_disables_visual_when_applitools_key_missing(self) -> None:
        with workspace_temp_dir() as temp_dir:
            executor = run_orchestration_test.TestExecutor(
                type="playwright",
                github_url="https://example.com/repo.git",
                name="specmatic-studio-playwright-ts-tests",
                branch="main",
                description="",
                workflow_globs=[],
                workflow_files=[],
                command=[],
                result_paths=[],
            )
            original_env = run_orchestration_test.os.environ.copy()
            try:
                run_orchestration_test.os.environ.pop("APPLITOOLS_API_KEY", None)
                env = run_orchestration_test.build_command_env(
                    repo_dir=temp_dir,
                    output_dir=temp_dir / "out",
                    workflow_file=".github/workflows/playwright-openapi-contract.yml",
                    executor=executor,
                )
            finally:
                run_orchestration_test.os.environ.clear()
                run_orchestration_test.os.environ.update(original_env)

            self.assertEqual(env.get("ENABLE_VISUAL"), "false")

    def test_build_command_env_includes_enterprise_and_jar_overrides(self) -> None:
        with workspace_temp_dir() as temp_dir:
            executor = run_orchestration_test.TestExecutor(
                type="sample-project",
                github_url="https://example.com/repo.git",
                name="repo",
                branch="main",
                description="",
                workflow_globs=[],
                workflow_files=[],
                command=[],
                result_paths=[],
            )
            env = run_orchestration_test.build_command_env(
                repo_dir=temp_dir,
                output_dir=temp_dir / "out",
                workflow_file=".github/workflows/gradle.yml",
                executor=executor,
                specmatic_version="2.0.0-SNAPSHOT",
                enterprise_version="3.0.0-SNAPSHOT",
                enterprise_docker_image="specmatic/enterprise:3.0.0-SNAPSHOT",
                specmatic_jar_url="https://example.com/specmatic.jar",
                specmatic_jar_path="C:/tmp/specmatic.jar",
            )
            self.assertNotIn("ORG_GRADLE_PROJECT_specmaticVersion", env)
            self.assertEqual(env.get("ORG_GRADLE_PROJECT_specmaticEnterpriseVersion"), "3.0.0-SNAPSHOT")
            self.assertEqual(env.get("ORG_GRADLE_PROJECT_enterpriseVersion"), "3.0.0-SNAPSHOT")
            self.assertEqual(env.get("SPECMATIC_JAR_URL"), "https://example.com/specmatic.jar")
            self.assertEqual(env.get("SPECMATIC_JAR_PATH"), "C:/tmp/specmatic.jar")
            self.assertEqual(env.get("ENTERPRISE_DOCKER_IMAGE"), "specmatic/enterprise:3.0.0-SNAPSHOT")
            self.assertEqual(env.get("SPECMATIC_STUDIO_DOCKER_IMAGE"), "specmatic/enterprise:3.0.0-SNAPSHOT")

    def test_build_command_env_for_sample_project_skips_specmatic_version(self) -> None:
        with workspace_temp_dir() as temp_dir:
            executor = run_orchestration_test.TestExecutor(
                type="sample-project",
                github_url="https://example.com/repo.git",
                name="repo",
                branch="main",
                description="",
                workflow_globs=[],
                workflow_files=[],
                command=[],
                result_paths=[],
            )
            env = run_orchestration_test.build_command_env(
                repo_dir=temp_dir,
                output_dir=temp_dir / "out",
                workflow_file=".github/workflows/gradle.yml",
                executor=executor,
                specmatic_version="2.0.0-SNAPSHOT",
                enterprise_version="3.0.0-SNAPSHOT",
            )
            self.assertNotIn("ORG_GRADLE_PROJECT_specmaticVersion", env)
            self.assertEqual(env.get("ORG_GRADLE_PROJECT_specmaticEnterpriseVersion"), "3.0.0-SNAPSHOT")
            self.assertEqual(env.get("ORG_GRADLE_PROJECT_enterpriseVersion"), "3.0.0-SNAPSHOT")

    def test_skips_enterprise_release_gate_workflow(self) -> None:
        with workspace_temp_dir() as temp_dir:
            workflow = temp_dir / ".github" / "workflows" / "playwright-enterprise-release-gate.yml"
            workflow.parent.mkdir(parents=True, exist_ok=True)
            workflow.write_text("name: gate\n", encoding="utf-8")

            self.assertIn(workflow.name.lower(), run_orchestration_test.SKIPPED_WORKFLOW_FILE_NAMES)


    def test_normalizes_gradle_wrapper_to_absolute_windows_path(self) -> None:
        with workspace_temp_dir() as temp_dir:
            gradlew_bat = temp_dir / "gradlew.bat"
            gradlew_bat.write_text("@echo off\n", encoding="utf-8")
            gradlew = temp_dir / "gradlew"
            gradlew.write_text("#!/bin/sh\n", encoding="utf-8")

            command = run_orchestration_test.normalize_command_for_os(["./gradlew", "test"], temp_dir)

            if os.name == "nt":
                self.assertEqual(command[0], str(gradlew_bat.resolve()))
            else:
                self.assertEqual(command[0], str(gradlew.resolve()))

    def test_normalizes_gradle_wrapper_to_absolute_posix_path_from_relative_repo(self) -> None:
        with workspace_temp_dir() as temp_dir:
            gradlew = temp_dir / "gradlew"
            gradlew.write_text("#!/bin/sh\n", encoding="utf-8")
            repo_dir = Path(os.path.relpath(temp_dir, Path.cwd()))

            with mock.patch.object(run_orchestration_test.os, "name", "posix"):
                command = run_orchestration_test.normalize_command_for_os(["./gradlew", "test"], repo_dir)

            self.assertEqual(command[0], str(gradlew.resolve()))

    def test_strips_outer_quotes_from_tests_filter_argument(self) -> None:
        with workspace_temp_dir() as temp_dir:
            gradlew_bat = temp_dir / "gradlew.bat"
            gradlew_bat.write_text("@echo off\n", encoding="utf-8")

            command = run_orchestration_test.normalize_command_for_os(
                ['./gradlew', '--tests="com.component.orders.contract.ContractTests"'],
                temp_dir,
            )

            self.assertIn("--tests=com.component.orders.contract.ContractTests", command)

    def test_extracts_commands_when_workflow_path_is_absolute_and_repo_path_is_relative(self) -> None:
        with workspace_temp_dir() as temp_dir:
            repo_dir = Path(os.path.relpath(temp_dir, Path.cwd()))
            workflow_dir = temp_dir / ".github" / "workflows"
            workflow_dir.mkdir(parents=True)
            workflow = workflow_dir / "gradle.yml"
            workflow.write_text(
                """
name: tests
jobs:
  test:
    steps:
      - run: ./gradlew test
""".lstrip(),
                encoding="utf-8",
            )

            commands = run_orchestration_test.extract_workflow_commands(workflow.resolve(), repo_dir)

            self.assertEqual([command.workflow_file for command in commands], [".github/workflows/gradle.yml"])

    def test_normalizes_current_and_legacy_executor_keys(self) -> None:
        executor = run_orchestration_test.normalize_executor(
            {
                "type": "playwright-studio",
                "github-url": "https://github.com/specmatic/specmatic-studio-playwright-ts-tests.git",
                "branch": "main",
                "workflowFiles": [".github/workflows/openapi-examples.yml"],
                "resultPaths": ["playwright-report"],
            },
            0,
        )

        self.assertEqual(executor.name, "specmatic-studio-playwright-ts-tests")
        self.assertEqual(executor.github_url, "https://github.com/specmatic/specmatic-studio-playwright-ts-tests.git")
        self.assertEqual(executor.branch, "main")
        self.assertEqual(executor.workflow_files, [".github/workflows/openapi-examples.yml"])
        self.assertEqual(executor.result_paths, ["playwright-report"])

    def test_normalize_executor_expands_env_placeholders_for_overrides(self) -> None:
        original_env = run_orchestration_test.os.environ.copy()
        try:
            run_orchestration_test.os.environ["ENTERPRISE_VERSION"] = "3.0.0-SNAPSHOT"
            run_orchestration_test.os.environ["ENTERPRISE_DOCKER_IMAGE"] = "specmatic/enterprise:3.0.0-SNAPSHOT"
            executor = run_orchestration_test.normalize_executor(
                {
                    "type": "playwright",
                    "github-url": "https://github.com/specmatic/specmatic-studio-playwright-ts-tests.git",
                    "enterprise-version": "${ENTERPRISE_VERSION}",
                    "enterprise-docker-image": "${ENTERPRISE_DOCKER_IMAGE}",
                },
                0,
            )
        finally:
            run_orchestration_test.os.environ.clear()
            run_orchestration_test.os.environ.update(original_env)

        self.assertEqual(executor.enterprise_version, "3.0.0-SNAPSHOT")
        self.assertEqual(executor.enterprise_docker_image, "specmatic/enterprise:3.0.0-SNAPSHOT")

    def test_collects_junit_counts_from_gradle_test_results(self) -> None:
        with workspace_temp_dir() as temp_dir:
            results_dir = temp_dir / "build" / "test-results" / "test"
            results_dir.mkdir(parents=True)
            (results_dir / "TEST-sample.xml").write_text(
                """
<testsuite tests="4" failures="1" errors="1" skipped="1">
  <testcase name="passes"/>
</testsuite>
""".lstrip(),
                encoding="utf-8",
            )

            total, failed, skipped = run_orchestration_test.collect_junit_counts(temp_dir)

            self.assertEqual((total, failed, skipped), (4, 2, 1))

    def test_collects_junit_counts_from_playwright_report(self) -> None:
        with workspace_temp_dir() as temp_dir:
            report_dir = temp_dir / "playwright-report"
            report_dir.mkdir(parents=True)
            (report_dir / "junit-report.xml").write_text(
                """
<testsuites tests="8" failures="1" skipped="2" errors="0">
  <testsuite name="suite-a" tests="8" failures="1" skipped="2" errors="0"/>
</testsuites>
""".lstrip(),
                encoding="utf-8",
            )

            total, failed, skipped = run_orchestration_test.collect_junit_counts(temp_dir)

            self.assertEqual((total, failed, skipped), (8, 1, 2))

    def test_marks_needs_cli_install_from_matrix_include(self) -> None:
        with workspace_temp_dir() as temp_dir:
            workflow = temp_dir / "workflow.yml"
            workflow.write_text(
                """
name: tests
jobs:
  test:
    strategy:
      matrix:
        include:
          - name: cli
            testName: ContractTestUsingCLITest
            needsCliInstall: true
    steps:
      - name: Matrix test
        run: ./gradlew test --tests="com.example.${{ matrix.testName }}"
""".lstrip(),
                encoding="utf-8",
            )

            commands = run_orchestration_test.extract_workflow_commands(workflow, temp_dir)

            self.assertEqual(len(commands), 1)
            self.assertTrue(commands[0].needs_cli_install)

    def test_prepare_cli_dependency_copies_jar_from_path(self) -> None:
        with workspace_temp_dir() as temp_dir:
            source_jar = temp_dir / "specmatic-enterprise.jar"
            with zipfile.ZipFile(source_jar, "w") as jar:
                jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            log_file = temp_dir / "run.log"
            target_jar = temp_dir / ".specmatic" / "specmatic-enterprise.jar"

            original_cli_jar_path = run_orchestration_test.cli_jar_path
            try:
                run_orchestration_test.cli_jar_path = lambda: target_jar
                ok, details = run_orchestration_test.prepare_cli_dependency(
                    run_orchestration_test.CliSetupConfig(
                        jar_url="",
                        jar_path=str(source_jar),
                        allow_installer=False,
                    ),
                    log_file=log_file,
                    dry_run=False,
                )
                self.assertTrue(ok)
                self.assertIn("copied Specmatic jar", details)
                self.assertTrue(target_jar.exists())
            finally:
                run_orchestration_test.cli_jar_path = original_cli_jar_path

    def test_prepare_cli_dependency_accepts_existing_target_jar_path(self) -> None:
        with workspace_temp_dir() as temp_dir:
            target_jar = temp_dir / ".specmatic" / "specmatic-enterprise.jar"
            target_jar.parent.mkdir(parents=True)
            with zipfile.ZipFile(target_jar, "w") as jar:
                jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            log_file = temp_dir / "run.log"

            original_cli_jar_path = run_orchestration_test.cli_jar_path
            try:
                run_orchestration_test.cli_jar_path = lambda: target_jar
                ok, details = run_orchestration_test.prepare_cli_dependency(
                    run_orchestration_test.CliSetupConfig(
                        jar_url="",
                        jar_path=str(target_jar),
                        allow_installer=False,
                    ),
                    log_file=log_file,
                    dry_run=False,
                )
                self.assertTrue(ok)
                self.assertIn("already present", details)
            finally:
                run_orchestration_test.cli_jar_path = original_cli_jar_path

    def test_write_enterprise_maven_repo_creates_executable_artifact(self) -> None:
        with workspace_temp_dir() as temp_dir:
            source_jar = temp_dir / "specmatic-enterprise.jar"
            with zipfile.ZipFile(source_jar, "w") as jar:
                jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            repo_dir = temp_dir / "maven"

            repo_url = run_orchestration_test.write_enterprise_maven_repo(
                repo_dir,
                source_jar,
                "1.12.1-SNAPSHOT",
            )

            artifact_dir = repo_dir / "io" / "specmatic" / "enterprise" / "executable" / "1.12.1-SNAPSHOT"
            self.assertEqual(repo_url, repo_dir.resolve().as_uri())
            self.assertTrue((artifact_dir / "executable-1.12.1-SNAPSHOT.jar").exists())
            self.assertTrue((artifact_dir / "executable-1.12.1-SNAPSHOT.pom").exists())



    def test_classifies_command_failure_with_test_failures_as_failed(self) -> None:
        status, details = run_orchestration_test.classify_final_status(
            run_orchestration_test.STATUS_COMMAND_FAILED,
            "workflow command failed",
            total_tests=10,
            failed_tests=2,
        )

        self.assertEqual(status, run_orchestration_test.STATUS_FAILED)
        self.assertEqual(details, "test failures detected")



    def test_keeps_command_failed_for_non_test_execution_failures(self) -> None:
        status, details = run_orchestration_test.classify_final_status(
            run_orchestration_test.STATUS_COMMAND_FAILED,
            "workflow command failed",
            total_tests=0,
            failed_tests=0,
        )

        self.assertEqual(status, run_orchestration_test.STATUS_COMMAND_FAILED)
        self.assertEqual(details, "workflow command failed")

    def test_renders_summary_table_with_test_counts(self) -> None:
        result = run_orchestration_test.WorkflowResult(
            type="sample-project",
            repository="specmatic-order-bff-java",
            repo_url="https://github.com/specmatic/specmatic-order-bff-java.git",
            branch="main",
            workflow=".github/workflows/gradle.yml",
            status="failed",
            exit_code=1,
            duration_seconds=12,
            commands=["./gradlew test"],
            executed_commands=[],
            output_dir="outputs/sample-project/specmatic-order-bff-java/gradle",
            log_file="outputs/sample-project/specmatic-order-bff-java/gradle/run.log",
            copied_result_paths=[],
            total_tests=8,
            failed_tests=2,
            skipped_tests=1,
            started_at="2026-04-22T05:00:00+00:00",
            finished_at="2026-04-22T05:00:12+00:00",
            details="command failed with exit code 1",
        )

        table = run_orchestration_test.render_summary_table([result])

        self.assertIn("Repository", table)
        self.assertIn("Completed in", table)
        self.assertIn("sample-project/specmatic-order-bff-java", table)
        self.assertIn("❌", table)
        self.assertIn("8", table)
        self.assertIn("2", table)
        self.assertNotIn("❌ failed", table)

    def test_log_progress_does_not_fail_on_cp1252_stdout(self) -> None:
        with tempfile.TemporaryFile("w+", encoding="cp1252") as stdout:
            with mock.patch("sys.stdout", stdout):
                run_orchestration_test.log_progress("✅ passed")
                stdout.seek(0)
                output = stdout.read()

        self.assertIn("? passed", output)

    def test_renders_parallel_progress_table_with_compact_statuses(self) -> None:
        pending = run_orchestration_test.ParallelWorkflowRun(
            workflow_label=".github/workflows/playwright-async-mock.yml",
            started_at="2026-04-22T05:00:00+00:00",
            dispatched_after=run_orchestration_test.datetime.now(run_orchestration_test.timezone.utc),
            ref="main",
            dispatch_started_monotonic=45.0,
            run_id=101,
            github_status="in_progress",
        )
        success = run_orchestration_test.ParallelWorkflowRun(
            workflow_label=".github/workflows/playwright-openapi-spec.yml",
            started_at="2026-04-22T05:00:00+00:00",
            dispatched_after=run_orchestration_test.datetime.now(run_orchestration_test.timezone.utc),
            ref="main",
            dispatch_started_monotonic=5.0,
            run_id=102,
            github_status="completed",
            conclusion="success",
            completed_run={"id": 102, "status": "completed", "conclusion": "success"},
        )

        with mock.patch.object(run_orchestration_test.time, "time", return_value=125.0):
            table = run_orchestration_test.render_parallel_progress_table([pending, success], polling_attempt=3)

        self.assertIn("Parallel workflow progress - Polling attempt 3", table)
        self.assertIn("playwright-async-mock", table)
        self.assertIn("pending", table)
        self.assertIn("success", table)
        self.assertIn("1m 20s", table)
        self.assertIn("completed (success)", table)
        self.assertTrue(table.startswith("\n"))
        self.assertTrue(table.endswith("\n"))
        self.assertIn("\n==================================================", table)

    def test_matches_dispatched_workflow_run_from_orchestrator_title_when_timestamp_skews(self) -> None:
        dispatched_after = run_orchestration_test.datetime(
            2026,
            5,
            7,
            10,
            0,
            0,
            tzinfo=run_orchestration_test.timezone.utc,
        )
        run = {
            "display_title": "Studio OpenAPI Generate Dictionary - Orchestrator #150",
            "created_at": "2026-05-07T10:00:10Z",
        }

        matched = run_orchestration_test.workflow_run_matches_dispatch(
            run,
            dispatched_after,
            expected_run_title_fragment="Orchestrator #150",
        )

        self.assertTrue(matched)

    def test_workflow_run_match_rejects_stale_title_match_before_dispatch_time(self) -> None:
        dispatched_after = run_orchestration_test.datetime(
            2026,
            5,
            7,
            10,
            0,
            0,
            tzinfo=run_orchestration_test.timezone.utc,
        )
        run = {
            "display_title": "Run tests - Orchestrator #150 retry 1",
            "created_at": "2026-05-07T09:59:40Z",
        }

        matched = run_orchestration_test.workflow_run_matches_dispatch(
            run,
            dispatched_after,
            expected_run_title_fragment="Orchestrator #150 retry 1",
        )

        self.assertFalse(matched)

    def test_run_executor_logs_dispatch_summary_and_progress_table(self) -> None:
        with workspace_temp_dir() as temp_dir:
            executor = run_orchestration_test.TestExecutor(
                type="playwright-tests",
                github_url="https://github.com/specmatic/specmatic-studio-playwright-ts-tests.git",
                name="repo",
                branch="main",
                description="",
                workflow_globs=[],
                workflow_files=[],
                command=[],
                result_paths=[],
            )

            logs: list[str] = []
            tick = {"value": 0.0}

            def fake_time() -> float:
                tick["value"] += 15.0
                return tick["value"]

            def fake_github_api_json(method: str, url: str, token: str, payload=None, ok_statuses=None):
                if "/actions/workflows/alpha.yml/runs" in url:
                    return {
                        "workflow_runs": [
                            {
                                "id": 101,
                                "status": "queued",
                                "conclusion": None,
                                "html_url": "https://example.com/101",
                                "created_at": "2026-12-22T05:00:05Z",
                            }
                        ]
                    }
                if "/actions/workflows/beta.yml/runs" in url:
                    return {
                        "workflow_runs": [
                            {
                                "id": 102,
                                "status": "queued",
                                "conclusion": None,
                                "html_url": "https://example.com/102",
                                "created_at": "2026-12-22T05:00:05Z",
                            }
                        ]
                    }
                if "/actions/runs/101" in url:
                    return {
                        "id": 101,
                        "status": "completed",
                        "conclusion": "success",
                        "html_url": "https://example.com/101",
                    }
                if "/actions/runs/102" in url:
                    return {
                        "id": 102,
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://example.com/102",
                    }
                raise AssertionError(f"Unexpected GitHub API url: {url}")

            remote_workflows = [
                run_orchestration_test.RemoteWorkflowFile(
                    label=".github/workflows/alpha.yml",
                    name="alpha.yml",
                    text="name: alpha\non:\n  workflow_dispatch:\njobs:\n  test:\n    steps:\n      - run: npm test\n",
                ),
                run_orchestration_test.RemoteWorkflowFile(
                    label=".github/workflows/beta.yml",
                    name="beta.yml",
                    text="name: beta\non:\n  workflow_dispatch:\njobs:\n  test:\n    steps:\n      - run: npm test\n",
                ),
            ]
            sleep_calls: list[int] = []

            def fake_sleep(seconds: int) -> None:
                sleep_calls.append(seconds)

            with mock.patch.object(run_orchestration_test, "discover_remote_workflow_files", return_value=remote_workflows), \
                mock.patch.object(run_orchestration_test, "dispatch_github_workflow", return_value=None), \
                mock.patch.object(run_orchestration_test, "github_api_json", side_effect=fake_github_api_json), \
                mock.patch.object(run_orchestration_test, "log_progress", side_effect=logs.append), \
                mock.patch.object(run_orchestration_test.time, "time", side_effect=fake_time), \
                mock.patch.object(run_orchestration_test.time, "sleep", side_effect=fake_sleep):
                results = run_orchestration_test.run_executor(
                    executor=executor,
                    outputs_dir=temp_dir / "outputs",
                    github_token="token",
                    api_base_url="https://api.github.com",
                    poll_seconds=1,
                    timeout_seconds=1000,
                )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].status, run_orchestration_test.STATUS_PASSED)
        self.assertEqual(results[1].status, run_orchestration_test.STATUS_FAILED)
        combined_logs = "\n".join(logs)
        self.assertIn("discovered 2 dispatchable workflow files", combined_logs)
        self.assertIn(
            "waiting 5s before dispatching workflow 2/2 for specmatic/specmatic-studio-playwright-ts-tests",
            combined_logs,
        )
        self.assertIn("Dispatched successfully: 2/2 workflows", combined_logs)
        self.assertIn("Parallel workflow progress - Polling attempt 1", combined_logs)
        self.assertIn("Parallel workflow progress - Polling attempt 2", combined_logs)
        self.assertIn("alpha", combined_logs)
        self.assertIn("beta", combined_logs)
        self.assertIn(5, sleep_calls)

    def test_main_dispatches_parallel_executors_in_batches_before_waiting(self) -> None:
        with workspace_temp_dir() as temp_dir:
            config_path = temp_dir / "test-executor.json"
            outputs_dir = temp_dir / "outputs"
            temp_repo_dir = temp_dir / "temp"
            config_path.write_text(
                json.dumps(
                    [
                        {
                            "type": "sample-project",
                            "name": "repo-a",
                            "github-url": "https://github.com/specmatic/repo-a.git",
                            "branch": "main",
                        },
                        {
                            "type": "sample-project",
                            "name": "repo-b",
                            "github-url": "https://github.com/specmatic/repo-b.git",
                            "branch": "main",
                        },
                        {
                            "type": "sample-project",
                            "name": "repo-c",
                            "github-url": "https://github.com/specmatic/repo-c.git",
                            "branch": "main",
                        },
                        {
                            "type": "sample-project",
                            "name": "repo-d",
                            "github-url": "https://github.com/specmatic/repo-d.git",
                            "branch": "main",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            events: list[str] = []

            def fake_dispatch(executor, outputs_dir, **_kwargs):
                events.append(f"dispatch:{executor.name}")
                return [], [
                    run_orchestration_test.ParallelWorkflowRun(
                        workflow_label=".github/workflows/gradle.yml",
                        started_at=run_orchestration_test.utc_now(),
                        dispatched_after=run_orchestration_test.datetime.now(run_orchestration_test.timezone.utc),
                        ref="main",
                        dispatch_started_monotonic=0.0,
                        executor=executor,
                        repo_slug=f"specmatic/{executor.name}",
                    )
                ]

            def fake_wait(dispatched, outputs_dir, **_kwargs):
                events.append(f"wait:{','.join(item.executor.name for item in dispatched if item.executor is not None)}")
                return [
                    run_orchestration_test.synthetic_result(
                        item.executor,
                        outputs_dir,
                        "gradle",
                        run_orchestration_test.STATUS_PASSED,
                        "ok",
                        0,
                    )
                    for item in dispatched
                    if item.executor is not None
                ]

            argv = [
                "run-orchestration-test.py",
                "--config",
                str(config_path),
                "--temp-dir",
                str(temp_repo_dir),
                "--outputs-dir",
                str(outputs_dir),
                "--enterprise-version",
                "0.0.0-DUMMY",
                "--parallel-batch-size",
                "3",
                "--specmatic-jar-url",
                "https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar",
            ]

            with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"ORCHESTRATOR_GITHUB_TOKEN": "token"}, clear=False), \
                mock.patch.object(run_orchestration_test, "dispatch_parallel_executor_workflows", side_effect=fake_dispatch), \
                mock.patch.object(run_orchestration_test, "wait_for_parallel_workflows", side_effect=fake_wait), \
                mock.patch.object(run_orchestration_test, "log_progress"):
                exit_code = run_orchestration_test.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                events,
                [
                    "dispatch:repo-a",
                    "dispatch:repo-b",
                    "dispatch:repo-c",
                    "wait:repo-a,repo-b,repo-c",
                    "dispatch:repo-d",
                    "wait:repo-d",
                ],
            )

    def test_main_retries_failed_zero_test_executor_once_after_all_batches_finish(self) -> None:
        with workspace_temp_dir() as temp_dir:
            config_path = temp_dir / "test-executor.json"
            outputs_dir = temp_dir / "outputs"
            temp_repo_dir = temp_dir / "temp"
            config_path.write_text(
                json.dumps(
                    [
                        {
                            "type": "sample-project",
                            "name": "repo-a",
                            "github-url": "https://github.com/specmatic/repo-a.git",
                            "branch": "main",
                        },
                        {
                            "type": "sample-project",
                            "name": "repo-b",
                            "github-url": "https://github.com/specmatic/repo-b.git",
                            "branch": "main",
                        },
                        {
                            "type": "sample-project",
                            "name": "repo-c",
                            "github-url": "https://github.com/specmatic/repo-c.git",
                            "branch": "main",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            events: list[str] = []
            wait_calls = 0

            def fake_dispatch(executor, outputs_dir, **_kwargs):
                events.append(f"dispatch:{executor.name}")
                return [], [
                    run_orchestration_test.ParallelWorkflowRun(
                        workflow_label=".github/workflows/gradle.yml",
                        started_at=run_orchestration_test.utc_now(),
                        dispatched_after=run_orchestration_test.datetime.now(run_orchestration_test.timezone.utc),
                        ref="main",
                        dispatch_started_monotonic=0.0,
                        executor=executor,
                        repo_slug=f"specmatic/{executor.name}",
                    )
                ]

            def fake_wait(dispatched, outputs_dir, **_kwargs):
                nonlocal wait_calls
                wait_calls += 1
                events.append(f"wait:{','.join(item.executor.name for item in dispatched if item.executor is not None)}")
                if wait_calls == 1:
                    return [
                        run_orchestration_test.synthetic_result(
                            dispatched[0].executor,
                            outputs_dir,
                            "gradle",
                            run_orchestration_test.STATUS_FAILED,
                            "GitHub Actions workflow_dispatch concluded with failure",
                            1,
                        ),
                        run_orchestration_test.synthetic_result(
                            dispatched[1].executor,
                            outputs_dir,
                            "gradle",
                            run_orchestration_test.STATUS_PASSED,
                            "ok",
                            0,
                        ),
                    ]
                if wait_calls == 2:
                    return [
                        run_orchestration_test.synthetic_result(
                            dispatched[0].executor,
                            outputs_dir,
                            "gradle",
                            run_orchestration_test.STATUS_PASSED,
                            "ok",
                            0,
                        )
                    ]
                return [
                    run_orchestration_test.synthetic_result(
                        dispatched[0].executor,
                        outputs_dir,
                        "gradle",
                        run_orchestration_test.STATUS_PASSED,
                        "ok",
                        0,
                    )
                ]

            argv = [
                "run-orchestration-test.py",
                "--config",
                str(config_path),
                "--temp-dir",
                str(temp_repo_dir),
                "--outputs-dir",
                str(outputs_dir),
                "--enterprise-version",
                "0.0.0-DUMMY",
                "--parallel-batch-size",
                "2",
                "--parallel-retry-delay-seconds",
                "10",
                "--parallel-retry-jitter-seconds",
                "0",
                "--specmatic-jar-url",
                "https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar",
            ]

            with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"ORCHESTRATOR_GITHUB_TOKEN": "token"}, clear=False), \
                mock.patch.object(run_orchestration_test, "dispatch_parallel_executor_workflows", side_effect=fake_dispatch), \
                mock.patch.object(run_orchestration_test, "wait_for_parallel_workflows", side_effect=fake_wait), \
                mock.patch.object(run_orchestration_test.time, "sleep") as mocked_sleep, \
                mock.patch.object(run_orchestration_test, "log_progress"):
                exit_code = run_orchestration_test.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                events,
                [
                    "dispatch:repo-a",
                    "dispatch:repo-b",
                    "wait:repo-a,repo-b",
                    "dispatch:repo-c",
                    "wait:repo-c",
                    "dispatch:repo-a",
                    "wait:repo-a",
                ],
            )
            mocked_sleep.assert_called_once_with(10)

    def test_workflow_result_from_github_run_downloads_artifacts_and_counts_junit(self) -> None:
        with workspace_temp_dir() as temp_dir:
            executor = run_orchestration_test.TestExecutor(
                type="playwright-tests",
                github_url="https://github.com/specmatic/specmatic-studio-playwright-ts-tests.git",
                name="repo",
                branch="main",
                description="",
                workflow_globs=[],
                workflow_files=[],
                command=[],
                result_paths=[],
            )
            archive_buffer = io.BytesIO()
            with zipfile.ZipFile(archive_buffer, "w") as archive:
                archive.writestr(
                    "playwright-report/junit-report.xml",
                    '<testsuites tests="2" failures="1" errors="0" skipped="1"><testsuite tests="2" failures="1" errors="0" skipped="1"/></testsuites>',
                )

            def fake_github_api_json(method: str, url: str, token: str, payload=None, ok_statuses=None):
                self.assertIn("/actions/runs/123/artifacts", url)
                return {
                    "artifacts": [
                        {
                            "id": 456,
                            "name": "playwright-report",
                            "archive_download_url": "https://api.example/artifacts/456/zip",
                            "expired": False,
                        }
                    ]
                }

            with mock.patch.object(run_orchestration_test, "github_api_json", side_effect=fake_github_api_json), \
                mock.patch.object(run_orchestration_test, "download_github_artifact_bytes", return_value=archive_buffer.getvalue()):
                result = run_orchestration_test.workflow_result_from_github_run(
                    executor=executor,
                    outputs_dir=temp_dir / "outputs",
                    repo_slug="specmatic/specmatic-studio-playwright-ts-tests",
                    workflow_label=".github/workflows/playwright-soap-spec.yml",
                    run={
                        "id": 123,
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://example.com/run/123",
                        "run_started_at": "2026-05-08T00:00:30Z",
                        "updated_at": "2026-05-08T00:02:05Z",
                    },
                    started_at="2026-05-08T00:00:00Z",
                    elapsed_seconds=300,
                    github_token="token",
                    api_base_url="https://api.github.com",
                )

            self.assertEqual(result.total_tests, 2)
            self.assertEqual(result.failed_tests, 1)
            self.assertEqual(result.skipped_tests, 1)
            self.assertEqual(result.duration_seconds, 95)
            self.assertEqual(result.started_at, "2026-05-08T00:00:30Z")
            self.assertEqual(result.finished_at, "2026-05-08T00:02:05Z")
            self.assertTrue(result.copied_result_paths)

    def test_workflow_result_from_github_run_dedupes_ctrf_counts_across_artifacts(self) -> None:
        with workspace_temp_dir() as temp_dir:
            executor = run_orchestration_test.TestExecutor(
                type="sample-project",
                github_url="https://github.com/specmatic/specmatic-order-bff-java.git",
                name="contract-tests",
                branch="main",
                description="",
                workflow_globs=[],
                workflow_files=[],
                command=[],
                result_paths=[],
            )

            def build_ctrf_archive(report_name: str) -> bytes:
                archive_buffer = io.BytesIO()
                with zipfile.ZipFile(archive_buffer, "w") as archive:
                    archive.writestr(
                        f"{report_name}/ctrf-report.json",
                        json.dumps(
                            {
                                "results": {
                                    "summary": {
                                        "tests": 2,
                                        "passed": 1,
                                        "failed": 1,
                                        "skipped": 0,
                                    },
                                    "tests": [
                                        {"id": "test-1", "name": "Contract 1", "status": "passed"},
                                        {"id": "test-2", "name": "Contract 2", "status": "failed"},
                                    ],
                                }
                            }
                        ),
                    )
                return archive_buffer.getvalue()

            archives = {
                "ctrf-report-docker": build_ctrf_archive("ctrf-report-docker"),
                "ctrf-report-cli": build_ctrf_archive("ctrf-report-cli"),
                "ctrf-report-junit": build_ctrf_archive("ctrf-report-junit"),
            }

            def fake_github_api_json(method: str, url: str, token: str, payload=None, ok_statuses=None):
                self.assertIn("/actions/runs/123/artifacts", url)
                return {
                    "artifacts": [
                        {
                            "id": 1,
                            "name": "ctrf-report-docker",
                            "archive_download_url": "https://api.example/artifacts/1/zip",
                            "expired": False,
                        },
                        {
                            "id": 2,
                            "name": "ctrf-report-cli",
                            "archive_download_url": "https://api.example/artifacts/2/zip",
                            "expired": False,
                        },
                        {
                            "id": 3,
                            "name": "ctrf-report-junit",
                            "archive_download_url": "https://api.example/artifacts/3/zip",
                            "expired": False,
                        },
                    ]
                }

            def fake_download(url: str, token: str) -> bytes:
                archive_id = url.rstrip("/").rsplit("/", 2)[-2]
                mapping = {
                    "1": archives["ctrf-report-docker"],
                    "2": archives["ctrf-report-cli"],
                    "3": archives["ctrf-report-junit"],
                }
                return mapping[archive_id]

            with mock.patch.object(run_orchestration_test, "github_api_json", side_effect=fake_github_api_json), \
                mock.patch.object(run_orchestration_test, "download_github_artifact_bytes", side_effect=fake_download):
                result = run_orchestration_test.workflow_result_from_github_run(
                    executor=executor,
                    outputs_dir=temp_dir / "outputs",
                    repo_slug="specmatic/specmatic-order-bff-java",
                    workflow_label=".github/workflows/gradle.yml",
                    run={
                        "id": 123,
                        "status": "completed",
                        "conclusion": "failure",
                        "html_url": "https://example.com/run/123",
                    },
                    started_at="2026-05-08T00:00:00Z",
                    elapsed_seconds=5,
                    github_token="token",
                    api_base_url="https://api.github.com",
                )

            self.assertEqual(result.total_tests, 2)
            self.assertEqual(result.failed_tests, 1)
            self.assertEqual(result.skipped_tests, 0)

    def test_download_github_artifact_bytes_follows_redirect_without_auth_header(self) -> None:
        archive_bytes = b"zip-bytes"

        class FakeOpener:
            def open(self, request, timeout=120):
                raise urllib.error.HTTPError(
                    request.full_url,
                    302,
                    "Found",
                    {"Location": "https://artifactcache.actions.githubusercontent.com/fake-signed-url"},
                    io.BytesIO(b""),
                )

        class FakeResponse:
            def __init__(self, body: bytes):
                self.body = body

            def read(self) -> bytes:
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(request, timeout=120):
            self.assertEqual(request.full_url, "https://artifactcache.actions.githubusercontent.com/fake-signed-url")
            self.assertIsNone(request.get_header("Authorization"))
            return FakeResponse(archive_bytes)

        with mock.patch.object(run_orchestration_test.urllib.request, "build_opener", return_value=FakeOpener()), \
            mock.patch.object(run_orchestration_test.urllib.request, "urlopen", side_effect=fake_urlopen):
            downloaded = run_orchestration_test.download_github_artifact_bytes(
                "https://api.github.com/repos/specmatic/repo/actions/artifacts/123/zip",
                "token",
            )

        self.assertEqual(downloaded, archive_bytes)

    def test_collect_test_counts_under_supports_report_format_fallbacks(self) -> None:
        with workspace_temp_dir() as temp_dir:
            playwright_dir = temp_dir / "playwright"
            playwright_dir.mkdir()
            (playwright_dir / "test-results.json").write_text(
                json.dumps(
                    {
                        "stats": {
                            "expected": 3,
                            "unexpected": 1,
                            "flaky": 1,
                            "skipped": 2,
                        }
                    }
                ),
                encoding="utf-8",
            )

            total, failed, skipped, report_format = run_orchestration_test.collect_test_counts_under(playwright_dir)

            self.assertEqual((total, failed, skipped, report_format), (7, 1, 2, "playwright-json"))

            ctrf_dir = temp_dir / "ctrf"
            ctrf_dir.mkdir()
            (ctrf_dir / "ctrf-report.json").write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {
                                "tests": 4,
                                "passed": 2,
                                "failed": 1,
                                "skipped": 1,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            total, failed, skipped, report_format = run_orchestration_test.collect_test_counts_under(ctrf_dir)

            self.assertEqual((total, failed, skipped, report_format), (4, 1, 1, "ctrf"))

    def test_github_conclusion_to_workflow_status_preserves_non_success_conclusions(self) -> None:
        self.assertEqual(
            run_orchestration_test.github_conclusion_to_workflow_status("success"),
            run_orchestration_test.STATUS_PASSED,
        )
        self.assertEqual(
            run_orchestration_test.github_conclusion_to_workflow_status("skipped"),
            run_orchestration_test.STATUS_SKIPPED,
        )
        self.assertEqual(
            run_orchestration_test.github_conclusion_to_workflow_status("cancelled"),
            run_orchestration_test.STATUS_CANCELLED,
        )
        self.assertEqual(
            run_orchestration_test.github_conclusion_to_workflow_status("timed_out"),
            run_orchestration_test.STATUS_TIMED_OUT,
        )
        self.assertEqual(
            run_orchestration_test.github_conclusion_to_workflow_status("action_required"),
            run_orchestration_test.STATUS_ACTION_REQUIRED,
        )

    def test_renders_consolidated_dashboard_and_workflow_page(self) -> None:
        with workspace_temp_dir() as temp_dir:
            output_dir = temp_dir / "outputs"
            workflow_output = output_dir / "sample-project" / "repo-a" / "gradle"
            workflow_output.mkdir(parents=True)
            (workflow_output / "run.log").write_text("log", encoding="utf-8")
            (workflow_output / "result.json").write_text("{}", encoding="utf-8")
            (workflow_output / "index.html").write_text("", encoding="utf-8")
            (workflow_output / "report.html").write_text("<html></html>", encoding="utf-8")

            result = run_orchestration_test.WorkflowResult(
                type="sample-project",
                repository="repo-a",
                repo_url="https://example.com/repo-a.git",
                branch="main",
                workflow=".github/workflows/gradle.yml",
                status="passed",
                exit_code=0,
                duration_seconds=4,
                commands=["./gradlew test"],
                executed_commands=[],
                output_dir=str(workflow_output),
                log_file=str(workflow_output / "run.log"),
                copied_result_paths=[],
                total_tests=5,
                failed_tests=0,
                skipped_tests=0,
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:04+00:00",
                details="ok",
            )

            summary = {
                "conclusion": "success",
                "numberOfReposIncluded": 1,
                "passed_count": 1,
                "failed_count": 0,
                "total_tests": 5,
                "failed_tests": 0,
                "skipped_tests": 0,
            }

            run_orchestration_test.render_html_reports(output_dir, summary, [result])

            self.assertTrue((output_dir / "index.html").exists())
            self.assertTrue((workflow_output / "index.html").exists())
            self.assertIn("repo-a", (output_dir / "index.html").read_text(encoding="utf-8"))

    def test_dashboard_moves_non_dispatchable_workflows_to_separate_section(self) -> None:
        with workspace_temp_dir() as temp_dir:
            output_dir = temp_dir / "outputs"
            runnable_output = output_dir / "sample-project" / "repo-a" / "gradle"
            skipped_output = output_dir / "playwright-tests" / "repo-b" / "playwright-async-spec"
            runnable_output.mkdir(parents=True)
            skipped_output.mkdir(parents=True)
            for workflow_output in (runnable_output, skipped_output):
                (workflow_output / "run.log").write_text("log", encoding="utf-8")

            runnable = run_orchestration_test.WorkflowResult(
                type="sample-project",
                repository="repo-a",
                repo_url="https://example.com/repo-a.git",
                branch="main",
                workflow=".github/workflows/gradle.yml",
                status=run_orchestration_test.STATUS_PASSED,
                exit_code=0,
                duration_seconds=4,
                commands=["./gradlew test"],
                executed_commands=[],
                output_dir=str(runnable_output),
                log_file=str(runnable_output / "run.log"),
                copied_result_paths=[],
                total_tests=5,
                failed_tests=0,
                skipped_tests=0,
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:04+00:00",
                details="ok",
            )
            non_dispatchable = run_orchestration_test.WorkflowResult(
                type="playwright-tests",
                repository="repo-b",
                repo_url="https://example.com/repo-b.git",
                branch="main",
                workflow=".github/workflows/playwright-async-spec.yml",
                status=run_orchestration_test.STATUS_SKIPPED,
                exit_code=0,
                duration_seconds=0,
                commands=[],
                executed_commands=[],
                output_dir=str(skipped_output),
                log_file=str(skipped_output / "run.log"),
                copied_result_paths=[],
                total_tests=0,
                failed_tests=0,
                skipped_tests=0,
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:00+00:00",
                details=".github/workflows/playwright-async-spec.yml cannot be dispatched because it does not declare workflow_dispatch.",
            )
            summary = run_orchestration_test.build_summary([runnable, non_dispatchable])

            run_orchestration_test.render_html_reports(output_dir, summary, [runnable, non_dispatchable])

            dashboard = (output_dir / "index.html").read_text(encoding="utf-8")
            main_table = dashboard.split("Skipped Workflows Without workflow_dispatch", maxsplit=1)[0]
            skipped_section = dashboard.split("Skipped Workflows Without workflow_dispatch", maxsplit=1)[1]
            self.assertIn("repo-a", main_table)
            self.assertNotIn("repo-b", main_table)
            self.assertIn("repo-b", skipped_section)
            self.assertIn("missing workflow_dispatch", skipped_section)

    def test_copy_result_paths_preserves_relative_structure(self) -> None:
        with workspace_temp_dir() as temp_dir:
            repo_dir = temp_dir / "repo"
            output_dir = temp_dir / "out"
            html_dir = repo_dir / "build" / "reports" / "specmatic" / "async" / "test" / "html"
            ctrf_dir = repo_dir / "build" / "reports" / "specmatic" / "grpc" / "test" / "ctrf"
            html_dir.mkdir(parents=True)
            ctrf_dir.mkdir(parents=True)
            (html_dir / "index.html").write_text("<html/>", encoding="utf-8")
            (ctrf_dir / "ctrf-report.json").write_text("{}", encoding="utf-8")

            copied = run_orchestration_test.copy_result_paths(
                repo_dir,
                output_dir,
                ["build/reports/specmatic/**/html", "build/reports/specmatic/**/ctrf"],
            )

            self.assertIn("build/reports/specmatic/async/test/html", copied)
            self.assertIn("build/reports/specmatic/grpc/test/ctrf", copied)
            self.assertTrue((output_dir / "build" / "reports" / "specmatic" / "async" / "test" / "html" / "index.html").exists())
            self.assertTrue((output_dir / "build" / "reports" / "specmatic" / "grpc" / "test" / "ctrf" / "ctrf-report.json").exists())

    def test_collect_report_file_entries_filters_to_specmatic_html_and_ctrf(self) -> None:
        with workspace_temp_dir() as temp_dir:
            output_dir = temp_dir / "out"
            spec_html = output_dir / "build" / "reports" / "specmatic" / "async" / "test" / "html" / "index.html"
            spec_ctrf = output_dir / "build" / "reports" / "specmatic" / "test" / "ctrf" / "ctrf-report.json"
            other_html = output_dir / "build" / "reports" / "tests" / "test" / "index.html"
            spec_html.parent.mkdir(parents=True)
            spec_ctrf.parent.mkdir(parents=True)
            other_html.parent.mkdir(parents=True)
            spec_html.write_text("<html/>", encoding="utf-8")
            spec_ctrf.write_text("{}", encoding="utf-8")
            other_html.write_text("<html/>", encoding="utf-8")

            html_files, ctrf_files, report_files = run_orchestration_test.collect_report_file_entries(output_dir)

            self.assertEqual([spec_html], html_files)
            self.assertEqual([spec_ctrf], ctrf_files)
            self.assertEqual(sorted([spec_ctrf, spec_html]), sorted(report_files))

    def test_collect_report_file_entries_includes_playwright_index_html(self) -> None:
        with workspace_temp_dir() as temp_dir:
            output_dir = temp_dir / "out"
            playwright_html = output_dir / "playwright-report" / "index.html"
            playwright_html.parent.mkdir(parents=True, exist_ok=True)
            playwright_html.write_text("<html/>", encoding="utf-8")

            html_files, ctrf_files, report_files = run_orchestration_test.collect_report_file_entries(output_dir)

            self.assertEqual([playwright_html], html_files)
            self.assertEqual([], ctrf_files)
            self.assertEqual([playwright_html], report_files)

    def test_copy_result_paths_tolerates_copytree_errors(self) -> None:
        with workspace_temp_dir() as temp_dir:
            repo_dir = temp_dir / "repo"
            output_dir = temp_dir / "out"
            report_dir = repo_dir / "test-results" / "suite-a"
            report_dir.mkdir(parents=True, exist_ok=True)
            (report_dir / "result.xml").write_text("<testsuite/>", encoding="utf-8")

            original_copytree = run_orchestration_test.shutil.copytree
            try:
                def raise_copytree(*args, **kwargs):
                    raise shutil.Error([("src", "dst", "simulated copytree failure")])

                run_orchestration_test.shutil.copytree = raise_copytree
                copied = run_orchestration_test.copy_result_paths(repo_dir, output_dir, ["test-results"])
            finally:
                run_orchestration_test.shutil.copytree = original_copytree

            self.assertIn("test-results", copied)
            self.assertTrue((output_dir / "test-results" / "suite-a" / "result.xml").exists())

    def test_pick_preferred_by_scope_prefers_build_reports_path(self) -> None:
        with workspace_temp_dir() as temp_dir:
            build_path = temp_dir / "build" / "reports" / "specmatic" / "test" / "html" / "index.html"
            legacy_path = temp_dir / "reports" / "specmatic" / "test" / "html" / "index.html"
            build_path.parent.mkdir(parents=True)
            legacy_path.parent.mkdir(parents=True)
            build_path.write_text("<html/>", encoding="utf-8")
            legacy_path.write_text("<html/>", encoding="utf-8")

            selected = run_orchestration_test.pick_preferred_by_scope([legacy_path, build_path], marker="html")

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0][0], "test")
            self.assertEqual(selected[0][1], build_path)

    def test_parse_ctrf_summary_reads_totals(self) -> None:
        with workspace_temp_dir() as temp_dir:
            ctrf_file = temp_dir / "ctrf-report.json"
            ctrf_file.write_text(
                """
{
  "results": {
    "summary": {
      "tests": 12,
      "passed": 10,
      "failed": 1,
      "skipped": 1
    }
  }
}
""".strip(),
                encoding="utf-8",
            )

            tests, passed, failed, skipped = run_orchestration_test.parse_ctrf_summary(ctrf_file)

            self.assertEqual((tests, passed, failed, skipped), (12, 10, 1, 1))


if __name__ == "__main__":
    unittest.main()
