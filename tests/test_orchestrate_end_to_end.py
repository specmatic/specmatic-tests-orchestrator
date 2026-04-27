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

from scripts.jar_fixture import build_minimal_jar_bytes


class _OrchestratorServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, _OrchestratorHandler)
        self.requests: list[dict[str, Any]] = []
        self.event = threading.Event()


class _OrchestratorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/enterprise.jar":
            body = build_minimal_jar_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/java-archive")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

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

        if len(self.server.requests) >= 1:  # type: ignore[attr-defined]
            self.server.event.set()  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class OrchestrateEndToEndTest(unittest.TestCase):
    def test_receives_trigger_runs_test_and_sends_callback(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            event_path = temp_path / "event.json"
            outputs_dir = temp_path / "outputs"
            consolidated_dir = temp_path / "consolidated_output"

            server = _OrchestratorServer(("127.0.0.1", 0))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            jar_url = f"http://127.0.0.1:{port}/enterprise.jar"
            event_path.write_text(
                json.dumps(
                    {
                        "action": "specmatic-enterprise-jar-ready",
                        "client_payload": {
                            "jar_url": jar_url,
                            "enterprise_repository": "specmatic/enterprise",
                            "enterprise_sha": "abc123def456",
                            "enterprise_run_id": "101",
                            "enterprise_run_attempt": "1",
                        },
                    }
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "GITHUB_EVENT_NAME": "repository_dispatch",
                    "GITHUB_EVENT_PATH": str(event_path),
                    "ENTERPRISE_CALLBACK_TOKEN": "dummy-token",
                    "GITHUB_API_BASE_URL": f"http://127.0.0.1:{port}",
                    "ORCHESTRATOR_TEST_EXECUTOR_PATH": str(repo_root / "tests" / "resources" / "test-executor-success.json"),
                    "SPEC_OUTPUTS_DIR": str(outputs_dir),
                    "SPEC_CONSOLIDATED_DIR": str(consolidated_dir),
                    "ORCHESTRATOR_RUN_URL": "http://example.local/orchestrator/run/1",
                    "ORCHESTRATOR_RUN_ID": "202",
                    "ORCHESTRATOR_RUN_ATTEMPT": "1",
                }
            )

            try:
                subprocess.run(
                    [sys.executable, "scripts/orchestrate.py"],
                    cwd=repo_root,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )

                self.assertTrue(server.event.wait(5), "timed out waiting for callback POST")
                self.assertEqual(len(server.requests), 1)

                self.assertTrue((outputs_dir / "sample-project-contract-tests" / "result.json").exists())
                self.assertTrue((outputs_dir / "sample-project-asyncapi-tests" / "result.json").exists())
                self.assertTrue((outputs_dir / "playwright-ui-tests" / "result.json").exists())
                self.assertTrue((consolidated_dir / "summary.json").exists())
                self.assertTrue((consolidated_dir / "summary.html").exists())

                finished = server.requests[0]
                self.assertEqual(finished["payload"]["event_type"], "specmatic-orchestrator-finished")
                self.assertEqual(finished["payload"]["client_payload"]["status"], "success")
                self.assertIn("summary_json", finished["payload"]["client_payload"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
