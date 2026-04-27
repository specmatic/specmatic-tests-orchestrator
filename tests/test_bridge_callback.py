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
    def __init__(self, server_address: tuple[str, int]) -> None:
        super().__init__(server_address, _CallbackHandler)
        self.requests: list[dict[str, Any]] = []
        self.event = threading.Event()


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

        if len(self.server.requests) >= 2:  # type: ignore[attr-defined]
            self.server.event.set()  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class BridgeCallbackTest(unittest.TestCase):
    def test_bridge_posts_check_run_and_dispatch_to_local_server(self) -> None:
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

                check_run = next(request for request in server.requests if request["path"].endswith("/check-runs"))
                dispatch = next(request for request in server.requests if request["path"].endswith("/dispatches"))

                self.assertEqual(check_run["payload"]["head_sha"], "abc123def456")
                self.assertEqual(check_run["payload"]["conclusion"], "success")
                self.assertEqual(check_run["payload"]["status"], "completed")
                self.assertEqual(dispatch["payload"]["event_type"], "specmatic-orchestrator-finished")
                self.assertEqual(dispatch["payload"]["client_payload"]["status"], "success")
                self.assertIn("summary_json", dispatch["payload"]["client_payload"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
