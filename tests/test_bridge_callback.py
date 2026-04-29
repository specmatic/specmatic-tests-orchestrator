from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


class _CallbackServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int], expected_requests: int = 2) -> None:
        super().__init__(server_address, _CallbackHandler)
        self.requests: list[dict[str, Any]] = []
        self.event = threading.Event()
        self.expected_requests = expected_requests


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        try:
            payload: Any = json.loads(body)
        except json.JSONDecodeError:
            payload = body

        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "headers": dict(self.headers),
                "payload": payload,
            }
        )

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

        if len(self.server.requests) >= self.server.expected_requests:  # type: ignore[attr-defined]
            self.server.event.set()  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class BridgeCallbackTest(unittest.TestCase):
    def test_bridge_posts_status_and_check_run_to_local_server(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            summary_path = temp_path / "summary.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "conclusion": "success",
                        "status": "success",
                        "total_sources": 1,
                        "passed_sources": 1,
                        "failed_sources": 0,
                        "total": 5,
                        "passed_count": 5,
                        "failed_count": 0,
                    }
                ),
                encoding="utf-8",
            )

            server = _CallbackServer(("127.0.0.1", 0))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            env = os.environ.copy()
            env.update(
                {
                    "SPECMATIC_SUMMARY_JSON": str(summary_path),
                    "ENTERPRISE_CALLBACK_TOKEN": "dummy-token",
                    "ENTERPRISE_REPOSITORY": "specmatic/enterprise",
                    "ENTERPRISE_SHA": "abc123def456",
                    "ENTERPRISE_RUN_ID": "101",
                    "ENTERPRISE_RUN_ATTEMPT": "1",
                    "ORCHESTRATOR_RUN_URL": "http://example.local/orchestrator/run/1",
                    "ORCHESTRATOR_RUN_ID": "202",
                    "ORCHESTRATOR_RUN_ATTEMPT": "1",
                    "GITHUB_API_BASE_URL": f"http://127.0.0.1:{port}",
                    "ENABLE_CHECK_RUNS": "true",
                }
            )

            try:
                subprocess.run(
                    [sys.executable, "scripts/bridge_to_enterprise.py"],
                    cwd=repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )

                self.assertTrue(server.event.wait(5), "timed out waiting for callback POSTs")
                self.assertEqual(len(server.requests), 2)

                status = next(request for request in server.requests if request["path"].endswith("/statuses/abc123def456"))
                check_run = next(request for request in server.requests if request["path"].endswith("/check-runs"))

                self.assertEqual(status["payload"]["state"], "success")
                self.assertEqual(status["payload"]["context"], "Orchestrator Gate for run 101 attempt 1")
                self.assertEqual(check_run["payload"]["head_sha"], "abc123def456")
                self.assertEqual(check_run["payload"]["conclusion"], "success")
                self.assertEqual(check_run["payload"]["status"], "completed")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_bridge_uses_orchestration_summary_for_workflow_summary_and_gate_status(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            summary_path = temp_path / "outputs" / "orchestration-summary.json"
            summary_path.parent.mkdir(parents=True)
            step_summary_path = temp_path / "step-summary.md"
            summary_path.write_text(
                json.dumps(
                    {
                        "conclusion": "failure",
                        "total": 2,
                        "passed_count": 0,
                        "failed_count": 2,
                        "total_tests": 233,
                        "failed_tests": 6,
                        "skipped_tests": 5,
                        "results": [
                            {
                                "type": "sample-project",
                                "repository": "contract-tests",
                                "workflow": ".github/workflows/gradle.yml",
                                "status": "failed",
                                "total_tests": 227,
                                "failed_tests": 5,
                                "skipped_tests": 4,
                            },
                            {
                                "type": "sample-project",
                                "repository": "asyncapi-tests",
                                "workflow": ".github/workflows/gradle.yml",
                                "status": "failed",
                                "total_tests": 6,
                                "failed_tests": 1,
                                "skipped_tests": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            server = _CallbackServer(("127.0.0.1", 0), expected_requests=1)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            env = os.environ.copy()
            env.update(
                {
                    "SPECMATIC_SUMMARY_JSON": str(summary_path),
                    "GITHUB_STEP_SUMMARY": str(step_summary_path),
                    "ENTERPRISE_CALLBACK_TOKEN": "dummy-token",
                    "ENTERPRISE_REPOSITORY": "specmatic/orchestrator-tester",
                    "ENTERPRISE_SHA": "abc123def456",
                    "ENTERPRISE_RUN_ID": "101",
                    "ENTERPRISE_RUN_ATTEMPT": "1",
                    "ORCHESTRATOR_RUN_URL": "http://example.local/orchestrator/run/1",
                    "ORCHESTRATOR_RUN_ID": "202",
                    "ORCHESTRATOR_RUN_ATTEMPT": "1",
                    "ENTERPRISE_STATUS_TARGET_URL": "http://example.local/enterprise/run/101",
                    "GITHUB_API_BASE_URL": f"http://127.0.0.1:{port}",
                    "ENABLE_CHECK_RUNS": "false",
                }
            )

            try:
                subprocess.run(
                    [sys.executable, "scripts/bridge_to_enterprise.py"],
                    cwd=repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )

                self.assertTrue(server.event.wait(5), "timed out waiting for status POST")
                self.assertEqual(len(server.requests), 1)
                status = next(request for request in server.requests if request["path"].endswith("/statuses/abc123def456"))

                self.assertEqual(status["payload"]["state"], "failure")
                self.assertEqual(status["payload"]["context"], "Orchestrator Gate for run 101 attempt 1")
                self.assertEqual(status["payload"]["target_url"], "http://example.local/enterprise/run/101")
                self.assertEqual(status["payload"]["description"], "Orchestrator run 202 failed")

                step_summary = step_summary_path.read_text(encoding="utf-8")
                self.assertIn("Specmatic Orchestration Result", step_summary)
                self.assertIn("| Total workflows | 2 |", step_summary)
                self.assertIn("| Total tests | 233 |", step_summary)
                self.assertIn("| Failed tests | 6 |", step_summary)
                self.assertIn("| sample-project/contract-tests | .github/workflows/gradle.yml | failed | 227 | 5 | 4 |", step_summary)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_summary_markdown_separates_workflow_counts_from_test_counts(self) -> None:
        summary = {
            "conclusion": "failure",
            "total": 1,
            "passed_count": 0,
            "failed_count": 1,
            "total_tests": 0,
            "failed_tests": 0,
            "skipped_tests": 0,
            "results": [
                {
                    "type": "sample-project",
                    "repository": "contract-tests",
                    "workflow": ".github/workflows/gradle.yml",
                    "status": "command_failed",
                    "total_tests": 0,
                    "failed_tests": 0,
                    "skipped_tests": 0,
                    "details": "3 command(s) failed",
                }
            ],
        }

        markdown = __import__("scripts.bridge_to_enterprise", fromlist=["summary_markdown"]).summary_markdown(
            summary,
            "failure",
            "http://example.local/orchestrator/run/1",
        )

        self.assertIn("| Total workflows | 1 |", markdown)
        self.assertIn("| Failed workflows | 1 |", markdown)
        self.assertIn("| Total tests | 0 |", markdown)
        self.assertIn("| Failed tests | 0 |", markdown)
        self.assertIn("| sample-project/contract-tests | .github/workflows/gradle.yml | command_failed | 0 | 0 | 0 | 3 command(s) failed |", markdown)

    def test_summary_markdown_uses_root_test_counts_before_nested_values(self) -> None:
        summary = {
            "conclusion": "failure",
            "total": 2,
            "passed_count": 0,
            "failed_count": 2,
            "total_tests": 233,
            "failed_tests": 6,
            "skipped_tests": 5,
            "results": [
                {
                    "type": "sample-project",
                    "repository": "contract-tests",
                    "workflow": ".github/workflows/gradle.yml",
                    "status": "failed",
                    "total_tests": 227,
                    "failed_tests": 5,
                    "skipped_tests": 4,
                    "duration_seconds": 118,
                },
                {
                    "type": "sample-project",
                    "repository": "asyncapi-tests",
                    "workflow": ".github/workflows/gradle.yml",
                    "status": "failed",
                    "total_tests": 6,
                    "failed_tests": 1,
                    "skipped_tests": 1,
                    "duration_seconds": 34,
                },
            ],
        }

        markdown = __import__("scripts.bridge_to_enterprise", fromlist=["summary_markdown"]).summary_markdown(
            summary,
            "failure",
            "http://example.local/orchestrator/run/1",
        )

        self.assertIn("| Total workflows | 2 |", markdown)
        self.assertIn("| Failed workflows | 2 |", markdown)
        self.assertIn("| Total tests | 233 |", markdown)
        self.assertIn("| Failed tests | 6 |", markdown)
        self.assertIn("| Skipped tests | 5 |", markdown)
        self.assertIn("| Duration | 152 |", markdown)


if __name__ == "__main__":
    unittest.main()
