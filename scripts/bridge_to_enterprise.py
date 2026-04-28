#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def load_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Summary JSON not found: {path}")

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Summary JSON is invalid: {exc}") from exc


def render_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


def pick_first(summary: Any, keys: Iterable[str]) -> Any:
    if isinstance(summary, dict):
        for key in keys:
            if key in summary:
                return summary[key]
        for value in summary.values():
            found = pick_first(value, keys)
            if found is not None:
                return found
    elif isinstance(summary, list):
        for value in summary:
            found = pick_first(value, keys)
            if found is not None:
                return found
    return None


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def infer_conclusion(summary: dict[str, Any]) -> str:
    def normalized(text: Any) -> str | None:
        if not isinstance(text, str):
            return None
        return text.strip().lower()

    direct = normalized(pick_first(summary, ["conclusion", "status", "result"]))
    if direct in {"success", "successful", "passed", "pass", "ok"}:
        return "success"
    if direct in {"failure", "failed", "fail", "error", "errored"}:
        return "failure"
    if direct in {"neutral", "skipped", "cancelled", "canceled", "action_required"}:
        return "neutral"

    passed = pick_first(summary, ["passed", "success"])
    if isinstance(passed, bool):
        return "success" if passed else "failure"

    failed_count = as_int(pick_first(summary, ["failed", "failed_count", "failures", "error_count"]))
    if failed_count is not None:
        return "failure" if failed_count > 0 else "success"

    total = as_int(pick_first(summary, ["total", "total_count", "tests_total"]))
    passed_count = as_int(pick_first(summary, ["passed_count", "success_count", "successful_count"]))
    if total is not None and passed_count is not None:
        return "success" if passed_count >= total else "failure"

    return "failure"


def summary_markdown(summary: dict[str, Any], conclusion: str, orchestrator_run_url: str) -> str:
    raw_summary = render_json(summary)
    excerpt = raw_summary if len(raw_summary) <= 3500 else raw_summary[:3450] + "\n... truncated for display ..."
    total = pick_first(summary, ["total", "total_count", "tests_total", "num_tests"])
    passed = pick_first(summary, ["passed", "passed_count", "success_count", "successful_count"])
    failed = pick_first(summary, ["failed", "failed_count", "failures", "error_count"])
    skipped = pick_first(summary, ["skipped", "skipped_count"])
    duration = pick_first(summary, ["duration", "elapsed", "elapsed_seconds", "runtime_seconds"])

    rows = [
        ("Conclusion", conclusion),
        ("Total", total if total is not None else "n/a"),
        ("Passed", passed if passed is not None else "n/a"),
        ("Failed", failed if failed is not None else "n/a"),
        ("Skipped", skipped if skipped is not None else "n/a"),
        ("Duration", duration if duration is not None else "n/a"),
        ("Orchestrator run", orchestrator_run_url),
    ]

    body = ["| Key | Value |", "| --- | --- |"]
    for key, value in rows:
        body.append(f"| {key} | {value} |")
    body.append("")
    body.append("Summary JSON excerpt:")
    body.append("```json")
    body.append(excerpt)
    body.append("```")
    return "\n".join(body)


def github_request(method: str, url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body) if response_body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed ({exc.code}): {body}") from exc


def create_check_run(
    token: str,
    repository: str,
    head_sha: str,
    conclusion: str,
    orchestrator_run_url: str,
    summary: dict[str, Any],
    api_base_url: str,
    enterprise_run_id: str | None,
    enterprise_run_attempt: str | None,
) -> None:
    run_id = enterprise_run_id or "unknown"
    attempt = enterprise_run_attempt or "unknown"
    payload = {
        "name": "Specmatic orchestrator",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "details_url": orchestrator_run_url,
        "output": {
            "title": f"Specmatic orchestrator for run {run_id} attempt {attempt}",
            "summary": summary_markdown(summary, conclusion, orchestrator_run_url),
        },
    }
    github_request("POST", f"{api_base_url}/repos/{repository}/check-runs", token, payload)


def dispatch_callback(
    token: str,
    repository: str,
    summary: dict[str, Any],
    conclusion: str,
    orchestrator_run_url: str,
    orchestrator_run_id: str,
    orchestrator_run_attempt: str,
    enterprise_sha: str,
    enterprise_run_id: str | None,
    enterprise_run_attempt: str | None,
    api_base_url: str,
) -> None:
    raw_summary = render_json(summary)
    payload = {
        "event_type": "specmatic-orchestrator-finished",
        "client_payload": {
            "status": conclusion,
            "enterprise_sha": enterprise_sha,
            "enterprise_run_id": enterprise_run_id,
            "enterprise_run_attempt": enterprise_run_attempt,
            "orchestrator_run_url": orchestrator_run_url,
            "orchestrator_run_id": orchestrator_run_id,
            "orchestrator_run_attempt": orchestrator_run_attempt,
            "summary_json": raw_summary if len(raw_summary) <= 7000 else None,
            "summary_json_truncated": len(raw_summary) > 7000,
            "summary_excerpt": raw_summary[:2000],
        },
    }
    github_request("POST", f"{api_base_url}/repos/{repository}/dispatches", token, payload)


def main() -> int:
    summary_path = Path(env("SPECMATIC_SUMMARY_JSON"))
    summary = load_summary(summary_path)
    conclusion = infer_conclusion(summary)

    callback_token = env("ENTERPRISE_CALLBACK_TOKEN")
    enterprise_repository = env("ENTERPRISE_REPOSITORY")
    enterprise_sha = env("ENTERPRISE_SHA")
    enterprise_run_id = os.environ.get("ENTERPRISE_RUN_ID")
    enterprise_run_attempt = os.environ.get("ENTERPRISE_RUN_ATTEMPT")
    orchestrator_run_url = env("ORCHESTRATOR_RUN_URL")
    orchestrator_run_id = env("ORCHESTRATOR_RUN_ID")
    orchestrator_run_attempt = env("ORCHESTRATOR_RUN_ATTEMPT")
    api_base_url = env("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")
    enable_check_runs = env("ENABLE_CHECK_RUNS", "false").strip().lower() in {"1", "true", "yes"}

    print(f"Inferred conclusion: {conclusion}")
    print(f"Enterprise repository: {enterprise_repository}")
    print(f"Enterprise SHA: {enterprise_sha}")

    errors: list[str] = []

    if enable_check_runs:
        try:
            print("Check run payload:")
            print(render_json({
                "repository": enterprise_repository,
                "head_sha": enterprise_sha,
                "conclusion": conclusion,
                "details_url": orchestrator_run_url,
                "summary_excerpt": summary_markdown(summary, conclusion, orchestrator_run_url),
            }))
            create_check_run(
                token=callback_token,
                repository=enterprise_repository,
                head_sha=enterprise_sha,
                conclusion=conclusion,
                orchestrator_run_url=orchestrator_run_url,
                summary=summary,
                api_base_url=api_base_url,
                enterprise_run_id=enterprise_run_id,
                enterprise_run_attempt=enterprise_run_attempt,
            )
            print("Created Enterprise check run.")
        except Exception as exc:
            errors.append(f"check run: {exc}")
    else:
        print("Skipping check run creation.")

    try:
        callback_payload = {
            "status": conclusion,
            "enterprise_sha": enterprise_sha,
            "enterprise_run_id": enterprise_run_id,
            "enterprise_run_attempt": enterprise_run_attempt,
            "orchestrator_run_url": orchestrator_run_url,
            "orchestrator_run_id": orchestrator_run_id,
            "orchestrator_run_attempt": orchestrator_run_attempt,
            "summary_json": render_json(summary),
        }
        print("Repository dispatch callback payload:")
        print(render_json(callback_payload))
        dispatch_callback(
            token=callback_token,
            repository=enterprise_repository,
            summary=summary,
            conclusion=conclusion,
            orchestrator_run_url=orchestrator_run_url,
            orchestrator_run_id=orchestrator_run_id,
            orchestrator_run_attempt=orchestrator_run_attempt,
            enterprise_sha=enterprise_sha,
            enterprise_run_id=enterprise_run_id,
            enterprise_run_attempt=enterprise_run_attempt,
            api_base_url=api_base_url,
        )
        print("Sent Enterprise repository_dispatch callback.")
    except Exception as exc:
        errors.append(f"repository_dispatch: {exc}")

    if errors:
        print("One or more callbacks failed:")
        for error in errors:
            print(f"- {error}")
        if len(errors) == 2:
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
