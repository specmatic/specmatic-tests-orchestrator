#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.consolidate_outputs import build_summary, load_source_results, render_markdown_summary, write_summary

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


def status_context(run_id: str | None, run_attempt: str | None) -> str:
    return f"Orchestrator Gate for run {run_id or 'unknown'} attempt {run_attempt or 'unknown'}"


def update_commit_status(
    token: str,
    repository: str,
    sha: str,
    state: str,
    orchestrator_run_url: str,
    description: str,
    api_base_url: str,
    context: str,
) -> dict[str, Any]:
    return github_request(
        "POST",
        f"{api_base_url}/repos/{repository}/statuses/{sha}",
        token,
        {
            "state": state,
            "target_url": orchestrator_run_url,
            "description": description[:140],
            "context": context,
        },
    )


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


def _to_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
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
    parsed = urlparse(jar_url)
    if parsed.scheme in {"http", "https"}:
        with urllib.request.urlopen(jar_url, timeout=60) as response, jar_path.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        return

    source = Path(unquote(parsed.path if parsed.scheme == "file" else jar_url)).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Jar not found: {source}")
    shutil.copyfile(source, jar_path)


def validate_jar(jar_path: Path) -> None:
    if not jar_path.exists():
        raise FileNotFoundError(f"Jar not found: {jar_path}")
    if jar_path.stat().st_size <= 0:
        raise ValueError(f"Jar is empty: {jar_path}")
    if not zipfile.is_zipfile(jar_path):
        raise ValueError(f"Invalid jar file (not a ZIP archive): {jar_path}")
    with zipfile.ZipFile(jar_path) as jar:
        if not jar.namelist():
            raise ValueError(f"Invalid jar file (no entries found): {jar_path}")


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


def resolve_sample_config_path(raw_path: str | None) -> Path:
    if not raw_path:
        return DEFAULT_SAMPLE_EXECUTORS

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (ROOT / candidate).resolve()

    if not candidate.exists():
        raise FileNotFoundError(f"Test executor manifest not found: {raw_path} (resolved to {candidate})")
    if not candidate.is_file():
        raise FileNotFoundError(f"Test executor manifest is not a file: {raw_path} (resolved to {candidate})")
    return candidate


def normalize_result(source: dict[str, Any], index: int, jar_url: str, jar_path: str) -> dict[str, Any]:
    result = source.get("result", {})
    if not isinstance(result, dict):
        result = {}

    total = _to_int(result.get("total"), [12, 8, 5][index % 3])
    passed = _to_bool(result.get("passed"), True)
    passed_count = _to_int(result.get("passed_count"), total if passed else max(total - 1, 0))
    failed_count = _to_int(result.get("failed_count"), max(total - passed_count, 0))
    delay_sec = _to_float(result.get("delay_sec"), 0.0)

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
        "delay_sec": delay_sec,
    }


def create_demo_source_results(outputs_dir: Path, jar_url: str, jar_path: str, config_path: Path) -> None:
    sources = load_sample_executors(config_path)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    for index, source in enumerate(sources):
        source_type = str(source.get("type", "sample-project"))
        source_name = str(source.get("name") or f"source-{index + 1}")
        source_dir = outputs_dir / f"{source_type}-{source_name}"
        source_dir.mkdir(parents=True, exist_ok=True)
        payload = normalize_result(source, index, jar_url, jar_path)
        if payload["delay_sec"] > 0:
            time.sleep(payload["delay_sec"])
        (source_dir / "result.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def github_request(method: str, url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(request, timeout=60) as response:
        response_body = response.read().decode("utf-8")
        return json.loads(response_body) if response_body else {}


def main() -> int:
    payload = load_event_payload()

    jar_url = os.environ.get("SPECMATIC_JAR_URL") or pick(payload, "jar_url")
    enterprise_repository = os.environ.get("ENTERPRISE_REPOSITORY") or pick(payload, "enterprise_repository", default="specmatic/enterprise")
    enterprise_sha = os.environ.get("ENTERPRISE_SHA") or pick(payload, "enterprise_sha")
    enterprise_run_id = os.environ.get("ENTERPRISE_RUN_ID") or pick(payload, "enterprise_run_id")
    enterprise_run_attempt = os.environ.get("ENTERPRISE_RUN_ATTEMPT") or pick(payload, "enterprise_run_attempt")
    enterprise_status_context = os.environ.get("ENTERPRISE_STATUS_CONTEXT") or pick(payload, "enterprise_status_context")
    status_token = os.environ.get("ENTERPRISE_CALLBACK_TOKEN", "")
    api_base_url = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")
    test_executor_path = os.environ.get("ORCHESTRATOR_TEST_EXECUTOR_PATH") or pick(payload, "test_executor_path")

    if not jar_url:
        raise SystemExit("SPECMATIC_JAR_URL or event payload jar_url is required")
    if not enterprise_repository:
        raise SystemExit("ENTERPRISE_REPOSITORY is required")
    if not enterprise_sha:
        raise SystemExit("ENTERPRISE_SHA or event payload enterprise_sha is required")

    outputs_dir = Path(os.environ.get("SPEC_OUTPUTS_DIR", "outputs"))
    consolidated_dir = Path(os.environ.get("SPEC_CONSOLIDATED_DIR", "consolidated_output"))

    summary: dict[str, Any] | None = None
    execution_error: str | None = None

    with tempfile.TemporaryDirectory(prefix="specmatic-") as temp_dir:
        jar_path = Path(temp_dir) / "enterprise.jar"
        try:
            sample_config = resolve_sample_config_path(test_executor_path)
            download_jar(jar_url, jar_path)
            validate_jar(jar_path)
            create_demo_source_results(outputs_dir, jar_url=jar_url, jar_path=str(jar_path), config_path=sample_config)
            summary = write_summary(outputs_dir=outputs_dir, consolidated_dir=consolidated_dir)
        except Exception as exc:
            execution_error = str(exc)
            consolidated_dir.mkdir(parents=True, exist_ok=True)
            partial_results = load_source_results(outputs_dir)
            summary = build_summary(partial_results)
            summary["conclusion"] = "failure"
            summary["status"] = "failure"
            summary["execution_error"] = execution_error
            summary["tests_skipped"] = True
            (consolidated_dir / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (consolidated_dir / "summary.html").write_text(
                f"<html><body><h1>Failed before summary generation</h1><pre>{execution_error}</pre></body></html>",
                encoding="utf-8",
            )
        finally:
            assert summary is not None
            if status_token:
                try:
                    state = "success" if summary["conclusion"] in {"success", "neutral"} else "failure"
                    context = enterprise_status_context or status_context(enterprise_run_id, enterprise_run_attempt)
                    status_response = update_commit_status(
                        token=status_token,
                        repository=enterprise_repository,
                        sha=enterprise_sha,
                        state=state,
                        orchestrator_run_url=os.environ.get("ORCHESTRATOR_RUN_URL", ""),
                        description=(
                            "Specmatic orchestrator completed successfully"
                            if state == "success"
                            else "Specmatic orchestrator completed with failures"
                        ),
                        api_base_url=api_base_url,
                        context=context,
                    )
                    print("Updated commit status.")
                    print(json.dumps(status_response, indent=2, sort_keys=True))
                except Exception as exc:
                    execution_error = execution_error or str(exc)
    if execution_error:
        print(f"Execution error: {execution_error}")

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path and summary is not None:
        with Path(step_summary_path).open("a", encoding="utf-8") as handle:
            handle.write(render_markdown_summary(summary, title="Specmatic Orchestrator Summary"))
            handle.write(
                "\n".join(
                    [
                        "",
                        f"## Final Status for enterprise run {enterprise_run_id or 'unknown'} attempt {enterprise_run_attempt or 'unknown'} (completed)",
                        "",
                        f"- Result: {'✅' if summary['conclusion'] == 'success' else '❌'} {summary['conclusion']}",
                        f"- Status context: `{enterprise_status_context or status_context(enterprise_run_id, enterprise_run_attempt)}`",
                        f"- Enterprise repo: `{enterprise_repository}`",
                        f"- Enterprise SHA: `{enterprise_sha}`",
                    ]
                )
                + "\n"
            )

    print(f"Downloaded jar from: {jar_url}")
    print(f"Used manifest: {sample_config}")
    print(f"Wrote source outputs to: {outputs_dir}")
    print(f"Wrote consolidated summary to: {consolidated_dir}")
    if summary is not None:
        print(json.dumps(summary, indent=2, sort_keys=True))

    if execution_error:
        return 1
    return 0 if summary and summary["failed_sources"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
