#!/usr/bin/env python3

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceResult:
    name: str
    passed: bool
    total: int
    passed_count: int
    failed_count: int
    details: dict[str, Any]


def _as_int(value: Any, default: int = 0) -> int:
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


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "passed", "pass", "success"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def load_source_results(outputs_dir: Path) -> list[SourceResult]:
    results: list[SourceResult] = []
    if not outputs_dir.exists():
        return results

    for source_dir in sorted(path for path in outputs_dir.iterdir() if path.is_dir()):
        result_file = source_dir / "result.json"
        if not result_file.exists():
            json_files = sorted(source_dir.glob("*.json"))
            result_file = json_files[0] if json_files else None
        if result_file is None or not result_file.exists():
            continue

        details = json.loads(result_file.read_text(encoding="utf-8"))
        name = str(details.get("source") or details.get("name") or source_dir.name)
        total = _as_int(details.get("total"), default=1)
        passed_count = _as_int(details.get("passed_count"), default=1 if _as_bool(details.get("passed"), True) else 0)
        failed_count = _as_int(details.get("failed_count"), default=max(total - passed_count, 0))
        passed = _as_bool(details.get("passed"), default=failed_count == 0)

        results.append(
            SourceResult(
                name=name,
                passed=passed,
                total=total,
                passed_count=passed_count,
                failed_count=failed_count,
                details=details,
            )
        )

    return results


def build_summary(results: list[SourceResult]) -> dict[str, Any]:
    total_sources = len(results)
    passed_sources = sum(1 for result in results if result.passed)
    failed_sources = total_sources - passed_sources
    total = sum(result.total for result in results)
    passed_count = sum(result.passed_count for result in results)
    failed_count = sum(result.failed_count for result in results)
    conclusion = "success" if failed_sources == 0 else "failure"

    return {
        "conclusion": conclusion,
        "status": conclusion,
        "total_sources": total_sources,
        "passed_sources": passed_sources,
        "failed_sources": failed_sources,
        "total": total,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "sources": [
            {
                "name": result.name,
                "passed": result.passed,
                "total": result.total,
                "passed_count": result.passed_count,
                "failed_count": result.failed_count,
                "details": result.details,
            }
            for result in results
        ],
    }


def render_html(summary: dict[str, Any]) -> str:
    rows = [
        ("Conclusion", summary["conclusion"]),
        ("Total sources", summary["total_sources"]),
        ("Passed sources", summary["passed_sources"]),
        ("Failed sources", summary["failed_sources"]),
        ("Total tests", summary["total"]),
        ("Passed tests", summary["passed_count"]),
        ("Failed tests", summary["failed_count"]),
    ]
    source_rows = "\n".join(
        f"<tr><td>{source['name']}</td><td>{'PASS' if source['passed'] else 'FAIL'}</td>"
        f"<td>{source['passed_count']}</td><td>{source['failed_count']}</td><td>{source['total']}</td></tr>"
        for source in summary["sources"]
    )
    metric_rows = "\n".join(f"<tr><th>{key}</th><td>{value}</td></tr>" for key, value in rows)
    summary_json = json.dumps(summary, indent=2, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Specmatic Consolidated Summary</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2937; }}
      h1, h2 {{ color: #0f172a; }}
      table {{ border-collapse: collapse; width: 100%; margin: 16px 0 24px; }}
      th, td {{ border: 1px solid #d1d5db; padding: 10px 12px; text-align: left; }}
      th {{ background: #f8fafc; }}
      .pass {{ color: #047857; font-weight: 700; }}
      .fail {{ color: #b91c1c; font-weight: 700; }}
      pre {{ background: #0f172a; color: #e2e8f0; padding: 16px; overflow: auto; border-radius: 8px; }}
    </style>
  </head>
  <body>
    <h1>Specmatic Consolidated Summary</h1>
    <table>
      <tbody>
        {metric_rows}
      </tbody>
    </table>
    <h2>Source Results</h2>
    <table>
      <thead>
        <tr><th>Source</th><th>Status</th><th>Passed</th><th>Failed</th><th>Total</th></tr>
      </thead>
      <tbody>
        {source_rows}
      </tbody>
    </table>
    <h2>JSON</h2>
    <pre>{summary_json}</pre>
  </body>
</html>
"""


def render_markdown_summary(summary: dict[str, Any], title: str = "Specmatic Consolidated Summary") -> str:
    passed = str(summary.get("conclusion", "")).strip().lower() in {"success", "neutral", "passed"}
    marker = "✅" if passed else "❌"
    rows = [
        f"# {marker} {title}",
        "",
        "## Totals",
        "",
        f"- Conclusion: `{summary.get('conclusion', '')}`",
        f"- Total sources: `{summary.get('total_sources', '')}`",
        f"- Passed sources: `{summary.get('passed_sources', '')}`",
        f"- Failed sources: `{summary.get('failed_sources', '')}`",
        f"- Total tests: `{summary.get('total', '')}`",
        f"- Passed tests: `{summary.get('passed_count', '')}`",
        f"- Failed tests: `{summary.get('failed_count', '')}`",
    ]

    sources = summary.get("sources", [])
    if isinstance(sources, list) and sources:
        rows.extend([
            "",
            "## Sources",
            "",
            "| Name | Status | Passed | Failed | Total |",
            "| --- | --- | ---: | ---: | ---: |",
        ])
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_name = str(source.get("name", ""))
            source_passed = bool(source.get("passed", False))
            rows.append(
                f"| {source_name} | {'✅ PASS' if source_passed else '❌ FAIL'} | {source.get('passed_count', '')} | {source.get('failed_count', '')} | {source.get('total', '')} |"
            )

    return "\n".join(rows) + "\n"


def write_summary(outputs_dir: Path, consolidated_dir: Path) -> dict[str, Any]:
    consolidated_dir.mkdir(parents=True, exist_ok=True)
    results = load_source_results(outputs_dir)
    summary = build_summary(results)

    (consolidated_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (consolidated_dir / "summary.html").write_text(render_html(summary), encoding="utf-8")
    return summary
