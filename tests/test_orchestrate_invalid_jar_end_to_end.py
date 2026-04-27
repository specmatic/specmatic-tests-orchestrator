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


class _InvalidJarServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, _InvalidJarHandler)
        self.requests: list[dict[str, Any]] = []
        self.event = threading.Event()


class _InvalidJarHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/enterprise.jar":
            body = b"not-a-jar"
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


class OrchestrateInvalidJarEndToEndTest(unittest.TestCase):
    def test_invalid_jar_aborts_before_tests_and_sends_failure_callback(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            event_path = temp_path / "event.json"
            outputs_dir = temp_path / "outputs"
            consolidated_dir = temp_path / "consolidated_output"

            server = _InvalidJarServer(("127.0.0.1", 0))
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
                result = subprocess.run(
                    [sys.executable, "scripts/orchestrate.py"],
                    cwd=repo_root,
                    env=env,
                    capture_output=True,
                    text=True,
                )

                self.assertNotEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
                self.assertTrue(server.event.wait(5), "timed out waiting for callback POST")
                self.assertEqual(len(server.requests), 1)

                finished = server.requests[0]
                self.assertEqual(finished["payload"]["event_type"], "specmatic-orchestrator-finished")
                self.assertEqual(finished["payload"]["client_payload"]["status"], "failure")
                self.assertIn("Invalid jar", finished["payload"]["client_payload"]["execution_error"])
                self.assertNotEqual(finished["payload"]["client_payload"]["phase"], "starting")

                self.assertFalse((outputs_dir / "sample-project-contract-tests" / "result.json").exists())
                self.assertTrue((consolidated_dir / "summary.json").exists())
                self.assertTrue((consolidated_dir / "summary.html").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
