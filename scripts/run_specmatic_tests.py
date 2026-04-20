#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.consolidate_outputs import write_summary


def create_demo_source_results(outputs_dir: Path, jar_url: str, jar_path: str) -> None:
    sources = {
        "api-tests": {
            "source": "api-tests",
            "passed": True,
            "total": 12,
            "passed_count": 12,
            "failed_count": 0,
            "jar_url": jar_url,
            "jar_path": jar_path,
        },
        "ui-tests": {
            "source": "ui-tests",
            "passed": True,
            "total": 8,
            "passed_count": 8,
            "failed_count": 0,
            "jar_url": jar_url,
            "jar_path": jar_path,
        },
    }

    outputs_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in sources.items():
        source_dir = outputs_dir / name
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "result.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    outputs_dir = Path(os.environ.get("SPEC_OUTPUTS_DIR", "outputs"))
    consolidated_dir = Path(os.environ.get("SPEC_CONSOLIDATED_DIR", "consolidated_output"))
    jar_url = os.environ.get("SPECMATIC_JAR_URL", "")
    jar_path = os.environ.get("SPECMATIC_JAR_PATH", "")

    create_demo_source_results(outputs_dir, jar_url=jar_url, jar_path=jar_path)
    summary = write_summary(outputs_dir=outputs_dir, consolidated_dir=consolidated_dir)

    print(f"Wrote source outputs to: {outputs_dir}")
    print(f"Wrote consolidated summary to: {consolidated_dir}")
    print(json.dumps(summary, indent=2, sort_keys=True))

    return 0 if summary["failed_sources"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
