#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
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


def top_level_int(summary: dict[str, Any], key: str) -> int | None:
    return as_int(summary.get(key))


def sum_result_ints(summary: dict[str, Any], key: str) -> int | None:
    results = summary.get("results")
    if not isinstance(results, list):
        return None

    total = 0
    found = False
    for result in results:
        if not isinstance(result, dict):
            continue
        value = as_int(result.get(key))
        if value is None:
            continue
        total += value
        found = True
    return total if found else None


def summary_count(summary: dict[str, Any], key: str) -> int | None:
    value = top_level_int(summary, key)
    if value is not None:
        return value
    return sum_result_ints(summary, key)


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
    total_workflows = summary_count(summary, "total")
    passed_workflows = summary_count(summary, "passed_count")
    failed_workflows = summary_count(summary, "failed_count")
    total_tests = summary_count(summary, "total_tests")
    failed_tests = summary_count(summary, "failed_tests")
    skipped_tests = summary_count(summary, "skipped_tests")
    duration = summary_count(summary, "duration_seconds")

    rows = [
        ("Conclusion", conclusion),
        ("Total workflows", total_workflows if total_workflows is not None else "n/a"),
        ("Passed workflows", passed_workflows if passed_workflows is not None else "n/a"),
        ("Failed workflows", failed_workflows if failed_workflows is not None else "n/a"),
        ("Total tests", total_tests if total_tests is not None else "n/a"),
        ("Failed tests", failed_tests if failed_tests is not None else "n/a"),
        ("Skipped tests", skipped_tests if skipped_tests is not None else "n/a"),
        ("Duration", duration if duration is not None else "n/a"),
        ("Orchestrator run", orchestrator_run_url),
    ]

    body = ["| Key | Value |", "| --- | --- |"]
    for key, value in rows:
        body.append(f"| {key} | {value} |")

    results = summary.get("results")
    if isinstance(results, list) and results:
        body.extend(
            [
                "",
                "Workflow results:",
                "",
                "| Repository | Workflow | Status | Tests | Failed | Skipped | Details |",
                "| --- | --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for result in results:
            if not isinstance(result, dict):
                continue
            repository = f"{result.get('type', '')}/{result.get('repository', '')}".strip("/")
            details = str(result.get("details") or "").replace("|", "\\|")
            if len(details) > 180:
                details = details[:177] + "..."
            body.append(
                "| "
                + " | ".join(
                    [
                        repository or "n/a",
                        str(result.get("workflow", "n/a")),
                        str(result.get("status", "n/a")),
                        str(result.get("total_tests", "n/a")),
                        str(result.get("failed_tests", "n/a")),
                        str(result.get("skipped_tests", "n/a")),
                        details or "n/a",
                    ]
                )
                + " |"
            )

    error_summary = summary.get("error_summary")
    if isinstance(error_summary, list) and error_summary:
        body.extend(
            [
                "",
                "Error summary and actionable steps:",
                "",
                "| Repository | Workflow | Status | Error | Action | Log |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for entry in error_summary:
            if not isinstance(entry, dict):
                continue
            body.append(
                "| "
                + " | ".join(
                    [
                        str(entry.get("repository", "n/a")).replace("|", "\\|"),
                        str(entry.get("workflow", "n/a")).replace("|", "\\|"),
                        str(entry.get("status", "n/a")).replace("|", "\\|"),
                        str(entry.get("error", "n/a")).replace("|", "\\|"),
                        str(entry.get("action", "n/a")).replace("|", "\\|"),
                        str(entry.get("log", "n/a")).replace("|", "\\|"),
                    ]
                )
                + " |"
            )

    body.append("")
    body.append(
        "Full details are available in the `specmatic-outputs` workflow artifact: "
        "`outputs/orchestration-summary.json` and `outputs/index.html`."
    )
    return "\n".join(body)


def compact_summary_markdown(summary: dict[str, Any], conclusion: str, orchestrator_run_url: str) -> str:
    total_workflows = summary_count(summary, "total")
    passed_workflows = summary_count(summary, "passed_count")
    failed_workflows = summary_count(summary, "failed_count")
    total_tests = summary_count(summary, "total_tests")
    failed_tests = summary_count(summary, "failed_tests")
    skipped_tests = summary_count(summary, "skipped_tests")
    duration = summary_count(summary, "duration_seconds")

    rows = [
        ("Conclusion", conclusion),
        ("Total workflows", total_workflows if total_workflows is not None else "n/a"),
        ("Passed workflows", passed_workflows if passed_workflows is not None else "n/a"),
        ("Failed workflows", failed_workflows if failed_workflows is not None else "n/a"),
        ("Total tests", total_tests if total_tests is not None else "n/a"),
        ("Failed tests", failed_tests if failed_tests is not None else "n/a"),
        ("Skipped tests", skipped_tests if skipped_tests is not None else "n/a"),
        ("Duration", duration if duration is not None else "n/a"),
        ("Orchestrator run", orchestrator_run_url),
    ]

    body = ["| Key | Value |", "| --- | --- |"]
    for key, value in rows:
        body.append(f"| {key} | {value} |")

    results = summary.get("results")
    if isinstance(results, list) and results:
        body.extend(
            [
                "",
                "Workflow results:",
                "",
                "| Repository | Workflow | Status | Tests | Failed | Skipped | Details |",
                "| --- | --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for result in results:
            if not isinstance(result, dict):
                continue
            repository = f"{result.get('type', '')}/{result.get('repository', '')}".strip("/")
            details = str(result.get("details") or "").replace("|", "\\|")
            if len(details) > 180:
                details = details[:177] + "..."
            body.append(
                "| "
                + " | ".join(
                    [
                        repository or "n/a",
                        str(result.get("workflow", "n/a")),
                        str(result.get("status", "n/a")),
                        str(result.get("total_tests", "n/a")),
                        str(result.get("failed_tests", "n/a")),
                        str(result.get("skipped_tests", "n/a")),
                        details or "n/a",
                    ]
                )
                + " |"
            )
    return "\n".join(body)


def append_workflow_summary(summary: dict[str, Any], conclusion: str, orchestrator_run_url: str) -> None:
    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not step_summary_path:
        return

    with Path(step_summary_path).open("a", encoding="utf-8") as handle:
        handle.write("## Specmatic Orchestration Result\n\n")
        handle.write(summary_markdown(summary, conclusion, orchestrator_run_url))
        handle.write("\n")


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


def github_curl_request(method: str, url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            method,
            "-H",
            f"Authorization: Bearer {token}",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload),
            "-w",
            "\n%{http_code}",
            url,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"curl failed ({completed.returncode}): {completed.stderr.strip()}")

    body, _, status_text = completed.stdout.rpartition("\n")
    try:
        status = int(status_text)
    except ValueError as exc:
        raise RuntimeError(f"curl returned an invalid HTTP status: {completed.stdout}") from exc

    if status >= 400:
        raise RuntimeError(f"GitHub API request failed ({status}): {body}")

    return json.loads(body) if body.strip() else {}


def status_context(run_id: str | None, run_attempt: str | None) -> str:
    return f"Orchestrator Gate for run {run_id or 'unknown'} attempt {run_attempt or 'unknown'}"


def update_commit_status(
    token: str,
    repository: str,
    sha: str,
    state: str,
    target_url: str,
    description: str,
    api_base_url: str,
    context: str,
) -> None:
    github_curl_request(
        "POST",
        f"{api_base_url}/repos/{repository}/statuses/{sha}",
        token,
        {
            "state": state,
            "target_url": target_url,
            "description": description[:140],
            "context": context,
        },
    )


def status_description(conclusion: str, orchestrator_run_id: str) -> str:
    outcome = "succeeded" if conclusion in {"success", "neutral"} else "failed"
    return f"Orchestrator run {orchestrator_run_id} {outcome}"


def compact_status_description(conclusion: str, orchestrator_run_id: str, summary: dict[str, Any]) -> str:
    total_tests = summary_count(summary, "total_tests")
    failed_tests = summary_count(summary, "failed_tests")
    skipped_tests = summary_count(summary, "skipped_tests")
    outcome = "succeeded" if conclusion in {"success", "neutral"} else "failed"
    if total_tests is None:
        return status_description(conclusion, orchestrator_run_id)
    failed = failed_tests if failed_tests is not None else 0
    skipped = skipped_tests if skipped_tests is not None else 0
    return f"Orchestrator run {orchestrator_run_id} {outcome}: {total_tests} tests, {failed} failed, {skipped} skipped"


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
    name: str,
) -> None:
    run_id = enterprise_run_id or "unknown"
    attempt = enterprise_run_attempt or "unknown"
    payload = {
        "name": name,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "details_url": orchestrator_run_url,
        "output": {
            "title": f"Specmatic orchestrator for run {run_id} attempt {attempt}",
            "summary": compact_summary_markdown(summary, conclusion, orchestrator_run_url),
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
    enterprise_status_context = os.environ.get("ENTERPRISE_STATUS_CONTEXT") or status_context(
        enterprise_run_id,
        enterprise_run_attempt,
    )
    enable_repository_dispatch = env("ENABLE_REPOSITORY_DISPATCH_CALLBACK", "false").strip().lower() in {"1", "true", "yes"}

    print(f"Inferred conclusion: {conclusion}")
    print(f"Enterprise repository: {enterprise_repository}")
    print(f"Enterprise SHA: {enterprise_sha}")
    append_workflow_summary(summary, conclusion, orchestrator_run_url)

    errors: list[str] = []

    try:
        update_commit_status(
            token=callback_token,
            repository=enterprise_repository,
            sha=enterprise_sha,
            state="success" if conclusion in {"success", "neutral"} else "failure",
            target_url=orchestrator_run_url,
            description=compact_status_description(conclusion, orchestrator_run_id, summary),
            api_base_url=api_base_url,
            context=enterprise_status_context,
        )
        print("Updated Enterprise commit status.")
    except Exception as exc:
        errors.append(f"commit status: {exc}")

    print("Skipping check run creation because this callback uses a PAT and the Checks API requires a GitHub App.")

    if enable_repository_dispatch:
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
    else:
        print("Skipping repository_dispatch callback.")

    if errors:
        print("One or more callbacks failed:")
        for error in errors:
            print(f"- {error}")
        if any(error.startswith("commit status:") for error in errors):
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
