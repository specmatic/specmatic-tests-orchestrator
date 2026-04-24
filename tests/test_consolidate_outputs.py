from __future__ import annotations

import json
import shutil
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from scripts.consolidate_outputs import load_source_results, write_summary


@contextmanager
def workspace_temp_dir():
    path = Path.cwd() / "temp" / "unit-tests" / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ConsolidateOutputsTest(unittest.TestCase):
    def test_writes_summary_json_and_html_for_multiple_sources(self) -> None:
        with workspace_temp_dir() as root:
            outputs_dir = root / "outputs"
            consolidated_dir = root / "consolidated_output"

            api_dir = outputs_dir / "api-tests"
            api_dir.mkdir(parents=True)
            (api_dir / "result.json").write_text(
                json.dumps(
                    {
                        "source": "api-tests",
                        "passed": True,
                        "total": 4,
                        "passed_count": 4,
                        "failed_count": 0,
                    }
                ),
                encoding="utf-8",
            )

            ui_dir = outputs_dir / "ui-tests"
            ui_dir.mkdir(parents=True)
            (ui_dir / "result.json").write_text(
                json.dumps(
                    {
                        "source": "ui-tests",
                        "passed": True,
                        "total": 3,
                        "passed_count": 3,
                        "failed_count": 0,
                    }
                ),
                encoding="utf-8",
            )

            summary = write_summary(outputs_dir, consolidated_dir)

            self.assertEqual(summary["conclusion"], "success")
            self.assertEqual(summary["total_sources"], 2)
            self.assertEqual(summary["passed_count"], 7)
            self.assertTrue((consolidated_dir / "summary.json").exists())
            self.assertTrue((consolidated_dir / "summary.html").exists())

            loaded_sources = load_source_results(outputs_dir)
            self.assertEqual([source.name for source in loaded_sources], ["api-tests", "ui-tests"])

    def test_summary_marks_failure_when_any_source_fails(self) -> None:
        with workspace_temp_dir() as root:
            outputs_dir = root / "outputs"
            consolidated_dir = root / "consolidated_output"

            failed_dir = outputs_dir / "api-tests"
            failed_dir.mkdir(parents=True)
            (failed_dir / "result.json").write_text(
                json.dumps(
                    {
                        "source": "api-tests",
                        "passed": False,
                        "total": 4,
                        "passed_count": 2,
                        "failed_count": 2,
                    }
                ),
                encoding="utf-8",
            )

            summary = write_summary(outputs_dir, consolidated_dir)

            self.assertEqual(summary["conclusion"], "failure")
            self.assertEqual(summary["failed_sources"], 1)
            self.assertEqual(summary["failed_count"], 2)


if __name__ == "__main__":
    unittest.main()
