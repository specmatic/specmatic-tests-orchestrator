#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.consolidate_outputs import write_summary

DEFAULT_SAMPLE_EXECUTORS = ROOT / "resources" / "test-executor.json"


def load_event_payload() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if not event_path:
        return {}

    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    if event_name == "repository_dispatch":
        return dict(event.get("client_payload", {}))
    if event_name == "workflow_dispatch":
        return dict(event.get("inputs", {}))
    return {}


def pick(payload: dict[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return default


def _to_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "passed", "pass", "success"}
    return default


def download_jar(jar_url: str, jar_path: Path) -> None:
    jar_path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(jar_url, timeout=60) as response, jar_path.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def load_sample_executors(config_path: Path) -> list[dict[str, Any]]:
    if not config_path.exists():
        return [
            {"type": "sample-project", "name": "contract-tests", "description": "Contract checks"},
            {"type": "sample-project", "name": "asyncapi-tests", "description": "AsyncAPI checks"},
            {"type": "playwright", "name": "ui-tests", "description": "UI checks"},
        ]

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def normalize_result(source: dict[str, Any], index: int, jar_url: str, jar_path: str) -> dict[str, Any]:
    result = source.get("result", {})
    if not isinstance(result, dict):
        result = {}

    total = _to_int(result.get("total"), [12, 8, 5][index % 3])
    passed = _to_bool(result.get("passed"), True)
    passed_count = _to_int(result.get("passed_count"), total if passed else max(total - 1, 0))
    failed_count = _to_int(result.get("failed_count"), max(total - passed_count, 0))

    return {
        "source": str(source.get("name") or f"source-{index + 1}"),
        "type": str(source.get("type", "sample-project")),
        "passed": passed,
        "total": total,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "jar_url": jar_url,
        "jar_path": jar_path,
        "description": source.get("description", ""),
        "result_kind": str(result.get("kind", "sample")),
    }


def create_demo_source_results(outputs_dir: Path, jar_url: str, jar_path: str, config_path: Path) -> None:
    sources = load_sample_executors(config_path)
    force_failure = os.environ.get("ORCHESTRATOR_SIMULATE_FAILURE", "").strip().lower() in {"1", "true", "yes", "failure"}

    outputs_dir.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(sources):
        source_type = str(source.get("type", "sample-project"))
        source_name = str(source.get("name") or f"source-{index + 1}")
        if force_failure and index == 0:
            source = dict(source)
            source_result = dict(source.get("result", {})) if isinstance(source.get("result"), dict) else {}
            source_result.update({"passed": False, "failed_count": max(_to_int(source_result.get("failed_count"), 0), 1)})
            source_result["passed_count"] = max(_to_int(source_result.get("total"), 1) - source_result["failed_count"], 0)
            source["result"] = source_result
        source_dir = outputs_dir / f"{source_type}-{source_name}"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "result.json").write_text(
            json.dumps(normalize_result(source, index, jar_url, jar_path), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    payload = load_event_payload()

    jar_url = os.environ.get("SPECMATIC_JAR_URL") or pick(payload, "jar_url")
    enterprise_repository = os.environ.get("ENTERPRISE_REPOSITORY") or pick(payload, "enterprise_repository", default="specmatic/enterprise")
    enterprise_sha = os.environ.get("ENTERPRISE_SHA") or pick(payload, "enterprise_sha")
    enterprise_run_id = os.environ.get("ENTERPRISE_RUN_ID") or pick(payload, "enterprise_run_id")
    enterprise_run_attempt = os.environ.get("ENTERPRISE_RUN_ATTEMPT") or pick(payload, "enterprise_run_attempt")

    if not jar_url:
        raise SystemExit("SPECMATIC_JAR_URL or event payload jar_url is required")
    if not enterprise_repository:
        raise SystemExit("ENTERPRISE_REPOSITORY is required")
    if not enterprise_sha:
        raise SystemExit("ENTERPRISE_SHA or event payload enterprise_sha is required")

    outputs_dir = Path(os.environ.get("SPEC_OUTPUTS_DIR", "outputs"))
    consolidated_dir = Path(os.environ.get("SPEC_CONSOLIDATED_DIR", "consolidated_output"))
    sample_config = Path(os.environ.get("ORCHESTRATOR_SAMPLE_CONFIG", DEFAULT_SAMPLE_EXECUTORS))

    with tempfile.TemporaryDirectory(prefix="specmatic-") as temp_dir:
        jar_path = Path(temp_dir) / "enterprise.jar"
        download_jar(jar_url, jar_path)

        create_demo_source_results(outputs_dir, jar_url=jar_url, jar_path=str(jar_path), config_path=sample_config)
        summary = write_summary(outputs_dir=outputs_dir, consolidated_dir=consolidated_dir)

    print(f"Downloaded jar from: {jar_url}")
    print(f"Wrote source outputs to: {outputs_dir}")
    print(f"Wrote consolidated summary to: {consolidated_dir}")
    print(json.dumps(summary, indent=2, sort_keys=True))

    env = os.environ.copy()
    env.update(
        {
            "SPECMATIC_SUMMARY_JSON": str(consolidated_dir / "summary.json"),
            "ENTERPRISE_REPOSITORY": enterprise_repository,
            "ENTERPRISE_SHA": enterprise_sha,
            "ENTERPRISE_RUN_ID": enterprise_run_id or "",
            "ENTERPRISE_RUN_ATTEMPT": enterprise_run_attempt or "",
            "ORCHESTRATOR_SIMULATE_FAILURE": os.environ.get("ORCHESTRATOR_SIMULATE_FAILURE", ""),
        }
    )

    subprocess.run([sys.executable, "scripts/bridge_to_enterprise.py"], cwd=ROOT, env=env, check=True)
    return 0 if summary["failed_sources"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
