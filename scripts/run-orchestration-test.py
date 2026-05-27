#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any


DEFAULT_CONFIG_PATH = Path("")
DEFAULT_WORKFLOW_GLOBS = [".github/workflows/*.yml", ".github/workflows/*.yaml"]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
DEFAULT_RESULT_PATHS = [
    "playwright-report",
    "blob-report",
    "test-results",
    "build/test-results",
    "build/reports/specmatic/**/html",
    "build/reports/specmatic/**/ctrf",
]

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_COMMAND_FAILED = "command_failed"
STATUS_MISSING_REPO_URL = "missing_repo_url"
STATUS_NO_WORKFLOWS = "no_workflows"
STATUS_NO_COMMANDS = "no_test_commands"
STATUS_SETUP_FAILED = "setup_failed"
STATUS_SKIPPED = "skipped"
STATUS_CANCELLED = "cancelled"
STATUS_TIMED_OUT = "timed_out"
STATUS_ACTION_REQUIRED = "action_required"
STATUS_NEUTRAL = "neutral"
STATUS_STARTUP_FAILURE = "startup_failure"
SKIPPED_WORKFLOW_FILE_NAMES = {
    "playwright-enterprise-release-gate.yml",
    "playwright-enterprise-release-gate.yaml",
}
PARALLEL_PROGRESS_LOG_INTERVAL_SECONDS = 60
WORKFLOW_RUN_DISCOVERY_CLOCK_SKEW_SECONDS = 300
PLAYWRIGHT_CONTAINER_NAMES = ["studio", "order-bff", "order-api", "inventory-api"]
ENTERPRISE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
ENTERPRISE_SNAPSHOT_REPO_URL = "https://central.sonatype.com/repository/maven-snapshots/io/specmatic/enterprise/executable-all"
ENTERPRISE_RELEASE_REPO_URL = "https://repo.specmatic.io/releases/io/specmatic/enterprise/executable-all"
ENTERPRISE_ARTIFACT_PATH_MARKERS = (
    "/io/specmatic/enterprise/executable/",
    "/io/specmatic/enterprise/executable-all",
)
PLAYWRIGHT_SERVICE_HEALTH_URLS = {
    "inventory-api": "http://127.0.0.1:8095/health",
    "order-api": "http://127.0.0.1:8090/products",
    "order-bff": "http://127.0.0.1:8080/health",
}

TEST_KEYWORDS = (
    " test",
    ":test",
    " build",
    " check",
    " verify",
    "integrationtest",
    "contracttest",
    "e2e",
    "pytest",
    "go test",
    "dotnet test",
)

SKIP_COMMAND_PREFIXES = (
    "echo ",
    "printf ",
    "pwd",
    "ls",
    "dir",
    "chmod ",
    "git ",
    "docker ",
    "curl ",
)


REUSABLE_WORKFLOW_USES_RE = re.compile(r"""^\s*uses:\s*["']?\./\.github/workflows/([^"'\s]+)["']?\s*$""")
INPUT_EXPRESSION_RE = re.compile(r"\$\{\{\s*inputs\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")
ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class TestExecutor:
    type: str
    github_url: str
    name: str
    branch: str
    description: str
    workflow_globs: list[str]
    workflow_files: list[str]
    command: list[str]
    result_paths: list[str]
    specmatic_version: str = ""
    enterprise_version: str = ""
    enterprise_docker_image: str = ""
    additional_env_variables: list[str] | None = None
    result_profile: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkflowCommand:
    workflow_file: str
    step_name: str
    command: str
    working_directory: str
    needs_cli_install: bool


@dataclass(frozen=True)
class CommandExecutionResult:
    workflow_file: str
    step_name: str
    command: str
    working_directory: str
    exit_code: int
    duration_seconds: int


@dataclass(frozen=True)
class WorkflowResult:
    type: str
    repository: str
    repo_url: str
    branch: str
    workflow: str
    status: str
    exit_code: int
    duration_seconds: int
    commands: list[str]
    executed_commands: list[CommandExecutionResult]
    output_dir: str
    log_file: str
    copied_result_paths: list[str]
    total_tests: int
    failed_tests: int
    skipped_tests: int
    started_at: str
    finished_at: str
    details: str


@dataclass(frozen=True)
class CliSetupConfig:
    jar_url: str
    jar_path: str
    allow_installer: bool
    snapshot_repo_url: str = ""


@dataclass(frozen=True)
class EnterpriseArtifact:
    version: str
    jar_url: str


@dataclass(frozen=True)
class ReusableWorkflowCall:
    workflow_path: str
    inputs: dict[str, str]


@dataclass
class ParallelWorkflowRun:
    workflow_label: str
    started_at: str
    dispatched_after: datetime
    ref: str
    dispatch_started_monotonic: float
    executor: TestExecutor | None = None
    repo_slug: str = ""
    expected_run_title_fragment: str = ""
    run_id: int | None = None
    html_url: str = ""
    github_status: str = "pending"
    conclusion: str = ""
    completed_run: dict[str, Any] | None = None
    error_message: str = ""


@dataclass(frozen=True)
class RemoteWorkflowFile:
    label: str
    name: str
    text: str


@dataclass(frozen=True)
class ParallelWorkflowSelection:
    dispatchable: list[RemoteWorkflowFile]
    non_dispatchable: list[RemoteWorkflowFile]


def parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export ") :].strip()
    if "=" not in stripped:
        return None
    key, _, raw_value = stripped.partition("=")
    key = key.strip()
    value = raw_value.strip()
    if not key:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_env_file(path: Path, override: bool = False) -> None:
    if not path.exists() or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(line)
        if not parsed:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value


def load_default_env_files() -> None:
    candidates = [
        Path(".env"),
        Path(".env.local"),
        Path("env/.env"),
        Path("env/.env.local"),
    ]
    for candidate in candidates:
        load_env_file(candidate, override=False)


def parse_args() -> argparse.Namespace:
    load_default_env_files()
    parser = argparse.ArgumentParser(description="Clone configured test repositories and run tests discovered from workflow files.")
    parser.add_argument("--config", default="", help="Path to test executor JSON.")
    parser.add_argument("--temp-dir", default="temp", help="Temporary directory cleaned before orchestration.")
    parser.add_argument("--outputs-dir", default=os.environ.get("SPEC_OUTPUTS_DIR", "outputs"))
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--specmatic-jar-url", default=os.environ.get("SPECMATIC_JAR_URL", ""))
    parser.add_argument("--specmatic-jar-path", default=os.environ.get("SPECMATIC_JAR_PATH", ""))
    parser.add_argument("--specmatic-version", default=os.environ.get("SPECMATIC_VERSION", ""))
    parser.add_argument("--enterprise-version", default=os.environ.get("ENTERPRISE_VERSION", ""))
    parser.add_argument("--enterprise-docker-image", default=os.environ.get("ENTERPRISE_DOCKER_IMAGE", ""))
    parser.add_argument("--snapshot-repo-url", default=os.environ.get("SNAPSHOT_REPO_URL", ""))
    parser.add_argument("--allow-cli-installer", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--parallel-poll-seconds", type=int, default=int(os.environ.get("PARALLEL_POLL_SECONDS", "30")))
    parser.add_argument("--parallel-timeout-seconds", type=int, default=int(os.environ.get("PARALLEL_TIMEOUT_SECONDS", "7200")))
    return parser.parse_args()


def validate_required_enterprise_version(args: argparse.Namespace) -> str:
    enterprise_version = (args.enterprise_version or os.environ.get("ENTERPRISE_VERSION", "")).strip()
    if not enterprise_version:
        return (
            "ENTERPRISE_VERSION is required but was not set. "
            "Set ENTERPRISE_VERSION in the environment or pass --enterprise-version."
        )
    if is_enterprise_repository_selector(enterprise_version):
        return ""
    return (
        "ENTERPRISE_VERSION must be one of: a Maven artifact version such as 1.12.1-SNAPSHOT, "
        "SNAPSHOT, RELEASE, a Specmatic Enterprise repository URL, or a direct Enterprise jar URL. "
        f"Got {enterprise_version!r}."
    )


def is_http_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_enterprise_repository_selector(value: str) -> bool:
    normalized = value.strip()
    if normalized.upper() in {"SNAPSHOT", "RELEASE"}:
        return True
    if ENTERPRISE_VERSION_RE.match(normalized):
        return True
    if not is_http_url(normalized):
        return False
    parsed = urllib.parse.urlsplit(normalized)
    path = parsed.path
    fragment = parsed.fragment
    return (
        parsed.netloc == "repo.specmatic.io"
        and any(marker in path or marker in fragment for marker in ENTERPRISE_ARTIFACT_PATH_MARKERS)
    )


def normalize_repo_browser_url(raw_url: str) -> str:
    parsed = urllib.parse.urlsplit(raw_url)
    if parsed.fragment.startswith("/"):
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.fragment, "", ""))
    return raw_url


def trim_url_slash(url: str) -> str:
    return url.rstrip("/")


def read_remote_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf-8")


def parse_xml_text(text: str, source_url: str) -> ET.Element:
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"could not parse Maven metadata from {source_url}: {exc}") from exc


def child_text(parent: ET.Element, path: str) -> str:
    element = parent.find(path)
    return (element.text or "").strip() if element is not None else ""


def latest_version_from_metadata(base_url: str) -> str:
    metadata_url = f"{trim_url_slash(base_url)}/maven-metadata.xml"
    root = parse_xml_text(read_remote_text(metadata_url), metadata_url)
    latest = child_text(root, "./versioning/latest") or child_text(root, "./versioning/release")
    if latest:
        return latest
    versions = [
        (version.text or "").strip()
        for version in root.findall("./versioning/versions/version")
        if (version.text or "").strip()
    ]
    if versions:
        return versions[-1]
    raise ValueError(f"could not find latest Enterprise version in {metadata_url}")


def latest_snapshot_jar_url(base_url: str, version: str) -> str:
    version_url = f"{trim_url_slash(base_url)}/{version}"
    metadata_url = f"{version_url}/maven-metadata.xml"
    root = parse_xml_text(read_remote_text(metadata_url), metadata_url)

    for snapshot_version in root.findall("./versioning/snapshotVersions/snapshotVersion"):
        extension = child_text(snapshot_version, "extension")
        classifier = child_text(snapshot_version, "classifier")
        value = child_text(snapshot_version, "value")
        if extension == "jar" and not classifier and value:
            return f"{version_url}/executable-all-{value}.jar"

    timestamp = child_text(root, "./versioning/snapshot/timestamp")
    build_number = child_text(root, "./versioning/snapshot/buildNumber")
    if timestamp and build_number and version.endswith("-SNAPSHOT"):
        base_version = version[: -len("-SNAPSHOT")]
        return f"{version_url}/executable-all-{base_version}-{timestamp}-{build_number}.jar"

    raise ValueError(f"could not find latest Enterprise snapshot jar in {metadata_url}")


def latest_release_jar_url(base_url: str, version: str) -> str:
    return f"{trim_url_slash(base_url)}/{version}/executable-all-{version}.jar"


def enterprise_version_from_jar_url(jar_url: str) -> str:
    parsed = urllib.parse.urlsplit(normalize_repo_browser_url(jar_url))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-1].endswith(".jar"):
        return parts[-2]
    raise ValueError(f"could not infer Enterprise version from jar URL: {jar_url}")


def resolve_enterprise_artifact_selector(selector: str) -> EnterpriseArtifact:
    raw = selector.strip()
    upper = raw.upper()
    if upper == "SNAPSHOT":
        version = latest_version_from_metadata(ENTERPRISE_SNAPSHOT_REPO_URL)
        return EnterpriseArtifact(version=version, jar_url=latest_snapshot_jar_url(ENTERPRISE_SNAPSHOT_REPO_URL, version))
    if upper == "RELEASE":
        version = latest_version_from_metadata(ENTERPRISE_RELEASE_REPO_URL)
        return EnterpriseArtifact(version=version, jar_url=latest_release_jar_url(ENTERPRISE_RELEASE_REPO_URL, version))
    if not is_http_url(raw):
        base_url = ENTERPRISE_SNAPSHOT_REPO_URL if raw.endswith("-SNAPSHOT") else ENTERPRISE_RELEASE_REPO_URL
        jar_url = latest_snapshot_jar_url(base_url, raw) if raw.endswith("-SNAPSHOT") else latest_release_jar_url(base_url, raw)
        return EnterpriseArtifact(version=raw, jar_url=jar_url)

    normalized_url = trim_url_slash(normalize_repo_browser_url(raw))
    if normalized_url.endswith(".jar"):
        return EnterpriseArtifact(version=enterprise_version_from_jar_url(normalized_url), jar_url=normalized_url)

    parts = [part for part in urllib.parse.urlsplit(normalized_url).path.split("/") if part]
    if parts and parts[-1] != "executable-all":
        version = parts[-1]
        base_url = normalized_url[: -len(version)].rstrip("/")
        jar_url = latest_snapshot_jar_url(base_url, version) if version.endswith("-SNAPSHOT") else latest_release_jar_url(base_url, version)
        return EnterpriseArtifact(version=version, jar_url=jar_url)

    is_release_repo = "/releases/" in urllib.parse.urlsplit(normalized_url).path
    version = latest_version_from_metadata(normalized_url)
    jar_url = latest_release_jar_url(normalized_url, version) if is_release_repo else latest_snapshot_jar_url(normalized_url, version)
    return EnterpriseArtifact(version=version, jar_url=jar_url)


def resolve_enterprise_artifact_inputs(enterprise_version: str, jar_url: str, jar_path: str) -> EnterpriseArtifact:
    if jar_url or jar_path:
        if not ENTERPRISE_VERSION_RE.match(enterprise_version):
            raise ValueError(
                "when --specmatic-jar-url or --specmatic-jar-path is provided, "
                "ENTERPRISE_VERSION must be a Maven artifact version such as 1.12.1-SNAPSHOT"
            )
        return EnterpriseArtifact(version=enterprise_version, jar_url=jar_url)
    return resolve_enterprise_artifact_selector(enterprise_version)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_template(template_name: str) -> Template:
    return Template((TEMPLATES_DIR / template_name).read_text(encoding="utf-8"))


def read_template_text(template_name: str) -> str:
    return (TEMPLATES_DIR / template_name).read_text(encoding="utf-8")


def html_escape(value: object) -> str:
    text = str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def relative_href(from_path: Path, to_path: Path) -> str:
    return os.path.relpath(to_path, from_path.parent).replace("\\", "/")


def log_progress(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe_message = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe_message, flush=True)


def compact_command(command: str, max_length: int = 120) -> str:
    command = " ".join(command.split())
    if len(command) <= max_length:
        return command
    return command[: max_length - 3] + "..."


def normalize_repo_name(repo_url: str) -> str:
    name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def normalize_command_list(raw_command: Any) -> list[str]:
    if isinstance(raw_command, str):
        return [raw_command]
    if isinstance(raw_command, list):
        return [str(item) for item in raw_command]
    return []


def normalize_additional_env_list(raw_env: Any) -> list[str]:
    if isinstance(raw_env, list):
        return [str(item).strip() for item in raw_env if str(item).strip()]
    return []


def validate_additional_env_variables(entries: list[str], executor_name: str) -> None:
    invalid_entries: list[str] = []
    for entry in entries:
        raw = entry.strip()
        if not raw or "=" not in raw:
            invalid_entries.append(raw)
            continue
        key, _ = raw.split("=", 1)
        key = key.strip()
        if not key:
            invalid_entries.append(raw)

    if invalid_entries:
        formatted = ", ".join(repr(item) for item in invalid_entries)
        raise ValueError(
            f"Invalid additional-env-variables for executor '{executor_name}'. "
            f"Each entry must be in KEY=VALUE format. Invalid entries: {formatted}"
        )


def expand_env_placeholders(value: str) -> str:
    if not value:
        return value
    return ENV_PLACEHOLDER_RE.sub(lambda match: os.environ.get(match.group(1), ""), value)


def normalize_executor(raw: dict[str, Any], index: int) -> TestExecutor:
    repo_url = expand_env_placeholders(str(raw.get("github-url") or raw.get("githubUrl") or raw.get("repoUrl") or raw.get("repo_url") or ""))
    repo_name = normalize_repo_name(repo_url)
    workflow_files = raw.get("workflow-files") or raw.get("workflowFiles") or raw.get("workflows") or []
    workflow_globs = raw.get("workflow-globs") or raw.get("workflowGlobs") or DEFAULT_WORKFLOW_GLOBS
    result_paths = raw.get("result-paths") or raw.get("resultPaths") or DEFAULT_RESULT_PATHS
    specmatic_version = expand_env_placeholders(str(raw.get("specmatic-version") or raw.get("specmaticVersion") or ""))
    enterprise_version = expand_env_placeholders(str(raw.get("enterprise-version") or raw.get("enterpriseVersion") or ""))
    enterprise_docker_image = expand_env_placeholders(
        str(raw.get("enterprise-docker-image") or raw.get("enterpriseDockerImage") or "")
    )
    additional_env_variables = normalize_additional_env_list(
        raw.get("additional-env-variables")
        or raw.get("additionalEnvVariables")
        or raw.get("additional_env_variables")
        or []
    )
    validate_additional_env_variables(additional_env_variables, repo_name)

    return TestExecutor(
        type=str(raw.get("type") or "default"),
        github_url=repo_url,
        name=repo_name,
        branch=str(raw.get("branch") or raw.get("ref") or ""),
        description=str(raw.get("description") or ""),
        workflow_globs=[str(item) for item in workflow_globs],
        workflow_files=[str(item) for item in workflow_files],
        command=normalize_command_list(raw.get("command") or []),
        result_paths=[str(item) for item in result_paths],
        specmatic_version=specmatic_version,
        enterprise_version=enterprise_version,
        enterprise_docker_image=enterprise_docker_image,
        additional_env_variables=additional_env_variables,
        result_profile=raw.get("result") if isinstance(raw.get("result"), dict) else None,
    )


def load_executors(config_path: Path) -> list[TestExecutor]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and any(key in raw for key in ["executors", "tests", "repositories"]):
        raw_items = raw.get("executors") or raw.get("tests") or raw.get("repositories") or []
    elif isinstance(raw, dict):
        raw_items = [value for value in raw.values() if isinstance(value, dict)]
    else:
        raw_items = raw

    if not isinstance(raw_items, list):
        raise ValueError(f"Expected a JSON array or object in {config_path}")

    return [normalize_executor(item, index) for index, item in enumerate(raw_items) if isinstance(item, dict)]


def resolve_config_path(raw_path: str) -> Path:
    if raw_path:
        return Path(raw_path)
    return DEFAULT_CONFIG_PATH


def handle_remove_readonly(func, path, excinfo) -> None:
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise excinfo[1]


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path, onerror=handle_remove_readonly)


def clean_run_directory(path: Path, label: str) -> None:
    resolved = path.resolve()
    unsafe_paths = {Path.cwd().resolve(), Path.home().resolve(), Path("/").resolve(), Path("/tmp").resolve()}
    if resolved in unsafe_paths:
        raise ValueError(f"refusing to clean unsafe {label} directory: {path}")
    log_progress(f"Cleaning {label} directory {path}")
    remove_tree(path)
    path.mkdir(parents=True, exist_ok=True)


def clean_temp_dir(temp_dir: Path) -> None:
    clean_run_directory(temp_dir, "temp")


def clean_outputs_dir(outputs_dir: Path) -> None:
    clean_run_directory(outputs_dir, "outputs")


def run_command(command: list[str], cwd: Path | None, env: dict[str, str] | None, log_file: Path) -> int:
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n$ {' '.join(command)}\n")
        log.flush()
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log.write(f"[exit_code={completed.returncode}]\n")
            return completed.returncode
        except FileNotFoundError as exc:
            log.write(f"[launcher_error] {exc}\n")
            return 127
        except OSError as exc:
            log.write(f"[launcher_error] {exc}\n")
            return 127


def append_log(log_file: Path, message: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"{message}\n")






def normalize_workflow_label(label: str) -> str:
    normalized = label.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def remote_workflow_matches_executor(path: str, executor: TestExecutor) -> bool:
    normalized_path = normalize_workflow_label(path)
    if executor.workflow_files:
        configured = {normalize_workflow_label(workflow_file) for workflow_file in executor.workflow_files}
        return normalized_path in configured
    return any(fnmatch.fnmatch(normalized_path, normalize_workflow_label(pattern)) for pattern in executor.workflow_globs)


def decode_github_content_file(item: dict[str, Any]) -> str:
    content = str(item.get("content") or "")
    encoding = str(item.get("encoding") or "")
    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8")
    return content


def discover_remote_workflow_files(
    repo_slug: str,
    ref: str,
    executor: TestExecutor,
    token: str,
    api_base_url: str,
) -> list[RemoteWorkflowFile]:
    if executor.workflow_files:
        workflow_items: list[dict[str, Any]] = []
        for workflow_file in executor.workflow_files:
            workflow_label = normalize_workflow_label(workflow_file)
            encoded_path = urllib.parse.quote(workflow_label, safe="/")
            query = urllib.parse.urlencode({"ref": ref})
            item = github_api_json(
                "GET",
                f"{api_base_url}/repos/{repo_slug}/contents/{encoded_path}?{query}",
                token,
            )
            if str(item.get("type") or "") == "file":
                workflow_items.append(item)
    else:
        query = urllib.parse.urlencode({"ref": ref})
        listing = github_api_json(
            "GET",
            f"{api_base_url}/repos/{repo_slug}/contents/.github/workflows?{query}",
            token,
        )
        if not isinstance(listing, list):
            raise RuntimeError(f"Expected .github/workflows listing for {repo_slug} to be a JSON array")
        workflow_items = [
            item
            for item in listing
            if isinstance(item, dict)
            and str(item.get("type") or "") == "file"
            and remote_workflow_matches_executor(str(item.get("path") or ""), executor)
        ]

    files: list[RemoteWorkflowFile] = []
    for item in workflow_items:
        path = normalize_workflow_label(str(item.get("path") or item.get("name") or ""))
        if not path:
            continue
        if "/" not in path:
            path = normalize_workflow_label(f".github/workflows/{path}")
        if "content" not in item:
            query = urllib.parse.urlencode({"ref": ref})
            encoded_path = urllib.parse.quote(path, safe="/")
            item = github_api_json(
                "GET",
                f"{api_base_url}/repos/{repo_slug}/contents/{encoded_path}?{query}",
                token,
            )
        files.append(RemoteWorkflowFile(label=path, name=Path(path).name, text=decode_github_content_file(item)))
    return sorted(files, key=lambda item: item.label)


def github_repo_slug(repo_url: str) -> str:
    parsed = urllib.parse.urlsplit(repo_url)
    path = parsed.path if parsed.scheme else repo_url
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return ""
    return f"{parts[-2]}/{parts[-1]}"


def github_api_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    ok_statuses: set[int] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    expected = ok_statuses or {200}
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            if response.status not in expected:
                raise RuntimeError(f"GitHub API returned HTTP {response.status}: {body}")
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed ({exc.code}): {body}") from exc


def github_api_bytes(method: str, url: str, token: str, ok_statuses: set[int] | None = None) -> bytes:
    request = urllib.request.Request(url, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    expected = ok_statuses or {200}
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            if response.status not in expected:
                raise RuntimeError(f"GitHub API returned HTTP {response.status}: {body.decode('utf-8', errors='replace')}")
            return body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed ({exc.code}): {body}") from exc


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def download_github_artifact_bytes(url: str, token: str) -> bytes:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=120) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code not in {301, 302, 303, 307, 308}:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API request failed ({exc.code}): {body}") from exc

        redirect_url = exc.headers.get("Location")
        if not redirect_url:
            raise RuntimeError(f"GitHub artifact download redirect missing Location header for {url}") from exc

        redirect_request = urllib.request.Request(redirect_url, method="GET")
        try:
            with urllib.request.urlopen(redirect_request, timeout=120) as response:
                return response.read()
        except urllib.error.HTTPError as redirect_exc:
            body = redirect_exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub artifact redirect download failed ({redirect_exc.code}): {body}") from redirect_exc


def safe_artifact_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip())
    return cleaned.strip(".-") or "artifact"


def download_github_run_artifacts(
    repo_slug: str,
    run_id: int,
    output_dir: Path,
    token: str,
    api_base_url: str,
    log_file: Path,
) -> list[Path]:
    try:
        payload = github_api_json(
            "GET",
            f"{api_base_url}/repos/{repo_slug}/actions/runs/{run_id}/artifacts?per_page=100",
            token,
        )
    except Exception as exc:
        append_log(log_file, f"Could not list workflow artifacts: {exc}")
        return []

    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        append_log(log_file, "No workflow artifacts found.")
        return []

    artifact_root = output_dir / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    extracted_paths: list[Path] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("expired"):
            continue
        artifact_id = artifact.get("id")
        if artifact_id in (None, ""):
            continue
        artifact_name = safe_artifact_name(str(artifact.get("name") or artifact_id))
        archive_url = str(artifact.get("archive_download_url") or "")
        if not archive_url:
            archive_url = f"{api_base_url}/repos/{repo_slug}/actions/artifacts/{artifact_id}/zip"

        archive_path = artifact_root / f"{artifact_name}.zip"
        extract_dir = artifact_root / artifact_name
        try:
            archive_bytes = download_github_artifact_bytes(archive_url, token)
            archive_path.write_bytes(archive_bytes)
            remove_tree(extract_dir)
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(extract_dir)
            extracted_paths.append(extract_dir)
            append_log(log_file, f"Downloaded artifact {artifact_name} to {extract_dir}")
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            append_log(log_file, f"Could not download artifact {artifact_name}: {exc}")
    return extracted_paths


def extract_workflow_dispatch_inputs_from_text(text: str) -> set[str]:
    lines = text.splitlines()
    inputs: set[str] = set()
    in_dispatch = False
    in_inputs = False
    dispatch_indent = -1
    inputs_indent = -1
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("workflow_dispatch:"):
            in_dispatch = True
            in_inputs = False
            dispatch_indent = indent
            continue
        if in_dispatch and indent <= dispatch_indent and not stripped.startswith("workflow_dispatch:"):
            break
        if in_dispatch and stripped.startswith("inputs:"):
            in_inputs = True
            inputs_indent = indent
            continue
        if in_inputs and indent <= inputs_indent and not stripped.startswith("inputs:"):
            break
        if in_inputs and indent > inputs_indent and stripped.endswith(":"):
            inputs.add(stripped[:-1].strip().strip("'\""))
    return inputs


def extract_workflow_dispatch_inputs(workflow_file: Path) -> set[str]:
    return extract_workflow_dispatch_inputs_from_text(workflow_file.read_text(encoding="utf-8"))


def has_workflow_dispatch_trigger_in_text(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.search(r"(^|[\s,\[-])workflow_dispatch\s*(:|,|\]|$)", stripped):
            return True
    return False


def has_workflow_dispatch_trigger(workflow_file: Path) -> bool:
    return has_workflow_dispatch_trigger_in_text(workflow_file.read_text(encoding="utf-8"))


def workflow_dispatch_inputs_for(
    available_inputs: set[str],
    specmatic_version: str,
    enterprise_version: str,
    enterprise_docker_image: str,
    jar_url: str,
    jar_path: str,
    orchestrator_disable_visual: str = "",
) -> dict[str, str]:
    github_run_number = os.environ.get("GITHUB_RUN_NUMBER", "")
    orchestrator_run_suffix = f"Orchestrator #{github_run_number}" if github_run_number else ""
    candidates = {
        "specmatic_version": specmatic_version,
        "enterprise_version": enterprise_version,
        "enterprise_docker_image": enterprise_docker_image,
        "specmatic_jar_url": jar_url,
        "enterprise_artifact_url": jar_url,
        "enterprise_jar_url": jar_url,
        "jar_url": jar_url,
        "specmatic_jar_path": jar_path,
        "orchestrator_disable_visual": orchestrator_disable_visual,
        "orchestrator_run_suffix": orchestrator_run_suffix,
    }
    return {
        key: value
        for key, value in candidates.items()
        if key in available_inputs and value
    }


def workflow_id_for_api(workflow_label: str) -> str:
    return urllib.parse.quote(Path(workflow_label).name, safe="")


def dispatch_github_workflow(
    repo_slug: str,
    workflow_label: str,
    ref: str,
    inputs: dict[str, str],
    token: str,
    api_base_url: str,
) -> None:
    payload: dict[str, Any] = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs
    github_api_json(
        "POST",
        f"{api_base_url}/repos/{repo_slug}/actions/workflows/{workflow_id_for_api(workflow_label)}/dispatches",
        token,
        payload,
        ok_statuses={204},
    )


def find_dispatched_workflow_run(
    repo_slug: str,
    workflow_label: str,
    branch: str,
    dispatched_after: datetime,
    token: str,
    api_base_url: str,
    timeout_seconds: int,
    poll_seconds: int,
    expected_run_title_fragment: str = "",
) -> dict[str, Any]:
    started = time.time()
    while time.time() - started < timeout_seconds:
        run = find_dispatched_workflow_run_once(
            repo_slug=repo_slug,
            workflow_label=workflow_label,
            branch=branch,
            dispatched_after=dispatched_after,
            token=token,
            api_base_url=api_base_url,
            expected_run_title_fragment=expected_run_title_fragment,
        )
        if run is not None:
            return run
        time.sleep(max(1, poll_seconds))
    raise TimeoutError(f"Timed out waiting for dispatched run for {repo_slug}/{workflow_label}")


def find_dispatched_workflow_run_once(
    repo_slug: str,
    workflow_label: str,
    branch: str,
    dispatched_after: datetime,
    token: str,
    api_base_url: str,
    expected_run_title_fragment: str = "",
) -> dict[str, Any] | None:
    workflow_id = workflow_id_for_api(workflow_label)
    query = urllib.parse.urlencode(
        {
            "event": "workflow_dispatch",
            "branch": branch,
            "per_page": "20",
        }
    )
    payload = github_api_json(
        "GET",
        f"{api_base_url}/repos/{repo_slug}/actions/workflows/{workflow_id}/runs?{query}",
        token,
    )
    for run in payload.get("workflow_runs", []):
        if workflow_run_matches_dispatch(run, dispatched_after, expected_run_title_fragment):
            return run
    return None


def workflow_run_matches_dispatch(
    run: dict[str, Any],
    dispatched_after: datetime,
    expected_run_title_fragment: str = "",
) -> bool:
    if expected_run_title_fragment:
        display_title = str(run.get("display_title") or run.get("name") or "")
        if expected_run_title_fragment.lower() in display_title.lower():
            return True

        created_at = str(run.get("created_at") or "")
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        return created >= dispatched_after

    created_at = str(run.get("created_at") or "")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return created.timestamp() >= dispatched_after.timestamp() - WORKFLOW_RUN_DISCOVERY_CLOCK_SKEW_SECONDS


def wait_for_github_workflow_run(
    repo_slug: str,
    run_id: int,
    token: str,
    api_base_url: str,
    timeout_seconds: int,
    poll_seconds: int,
    workflow_label: str = "",
) -> dict[str, Any]:
    started = time.time()
    last_status: str | None = None
    last_conclusion: str | None = None
    poll_count = 0
    while time.time() - started < timeout_seconds:
        run = github_api_json("GET", f"{api_base_url}/repos/{repo_slug}/actions/runs/{run_id}", token)
        poll_count += 1
        status = str(run.get("status") or "unknown")
        conclusion = str(run.get("conclusion") or "")
        if status != last_status or conclusion != last_conclusion or poll_count % 5 == 0:
            elapsed = int(time.time() - started)
            label = workflow_label or str(run_id)
            summary = f"     polling {label}: status={status}"
            if conclusion:
                summary += f", conclusion={conclusion}"
            summary += f", elapsed={elapsed}s"
            log_progress(summary)
            last_status = status
            last_conclusion = conclusion
        if run.get("status") == "completed":
            return run
        time.sleep(max(1, poll_seconds))
    raise TimeoutError(f"Timed out waiting for GitHub workflow run {run_id} in {repo_slug}")


def format_elapsed_time(seconds: int) -> str:
    minutes, remaining_seconds = divmod(max(0, seconds), 60)
    hours, remaining_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {remaining_minutes:02d}m {remaining_seconds:02d}s"
    if minutes:
        return f"{minutes}m {remaining_seconds:02d}s"
    return f"{remaining_seconds}s"


def parse_github_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def workflow_run_duration_seconds(run: dict[str, Any], fallback_seconds: int) -> int:
    started = parse_github_datetime(run.get("run_started_at")) or parse_github_datetime(run.get("created_at"))
    finished = parse_github_datetime(run.get("updated_at")) or parse_github_datetime(run.get("completed_at"))
    if started is None or finished is None:
        return max(0, fallback_seconds)
    return max(0, int((finished - started).total_seconds()))


def parallel_workflow_status(item: ParallelWorkflowRun) -> str:
    if item.error_message:
        return "failed"
    if item.completed_run is not None:
        conclusion = str(item.completed_run.get("conclusion") or item.conclusion or "").lower()
        return github_conclusion_to_workflow_status(conclusion)
    return "pending"


def parallel_workflow_detail(item: ParallelWorkflowRun) -> str:
    if item.error_message:
        return compact_command(item.error_message, 60)
    if item.completed_run is not None:
        conclusion = str(item.completed_run.get("conclusion") or item.conclusion or "completed")
        return f"completed ({conclusion})"
    if item.run_id is None:
        return "dispatched"
    return item.github_status or "pending"


def render_parallel_progress_table(items: list[ParallelWorkflowRun], polling_attempt: int) -> str:
    headers = ["Repository", "Workflow", "Status", "Elapsed", "Details"]
    rows: list[list[str]] = []
    for item in sorted(items, key=lambda current: (current.repo_slug.lower(), current.workflow_label.lower())):
        rows.append(
            [
                item.repo_slug or "n/a",
                Path(item.workflow_label).stem,
                parallel_workflow_status(item),
                format_elapsed_time(int(time.time() - item.dispatch_started_monotonic)),
                parallel_workflow_detail(item),
            ]
        )
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index])
        for index in range(len(headers))
    ]

    def render_row(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    separator = "-+-".join("-" * width for width in widths)
    block_separator = "=" * len(separator)
    return "\n".join(
        [
            "",
            block_separator,
            f"Parallel workflow progress - Polling attempt {polling_attempt}",
            render_row(headers),
            separator,
            *(render_row(row) for row in rows),
            block_separator,
            "",
        ]
    )


def github_conclusion_to_workflow_status(conclusion: str) -> str:
    normalized = conclusion.strip().lower()
    if normalized == "success":
        return STATUS_PASSED
    if normalized == "skipped":
        return STATUS_SKIPPED
    if normalized == "cancelled":
        return STATUS_CANCELLED
    if normalized == "timed_out":
        return STATUS_TIMED_OUT
    if normalized == "action_required":
        return STATUS_ACTION_REQUIRED
    if normalized == "neutral":
        return STATUS_NEUTRAL
    if normalized == "startup_failure":
        return STATUS_STARTUP_FAILURE
    return STATUS_FAILED


def workflow_result_from_github_run(
    executor: TestExecutor,
    outputs_dir: Path,
    repo_slug: str,
    workflow_label: str,
    run: dict[str, Any],
    started_at: str,
    elapsed_seconds: int,
    github_token: str,
    api_base_url: str,
) -> WorkflowResult:
    workflow = Path(workflow_label).stem
    output_dir = outputs_dir / executor.type / executor.name / workflow
    log_file = output_dir / "run.log"
    output_dir.mkdir(parents=True, exist_ok=True)
    conclusion = str(run.get("conclusion") or "failure")
    status = github_conclusion_to_workflow_status(conclusion)
    html_url = str(run.get("html_url") or "")
    actual_started_at = str(run.get("run_started_at") or run.get("created_at") or started_at)
    actual_finished_at = str(run.get("updated_at") or run.get("completed_at") or utc_now())
    actual_duration_seconds = workflow_run_duration_seconds(run, elapsed_seconds)
    details = f"GitHub Actions workflow_dispatch concluded with {conclusion}"
    if html_url:
        details = f"{details}; details: {html_url}"
    log_file.write_text(
        "\n".join(
            [
                f"GitHub Actions workflow: {workflow_label}",
                f"Run id: {run.get('id', 'n/a')}",
                f"Run URL: {html_url or 'n/a'}",
                f"Status: {run.get('status', 'n/a')}",
                f"Conclusion: {conclusion}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    artifact_paths: list[Path] = []
    total_tests = 0
    failed_tests = 0
    skipped_tests = 0
    run_id = run.get("id")
    if run_id not in (None, ""):
        artifact_paths = download_github_run_artifacts(
            repo_slug=repo_slug,
            run_id=int(run_id),
            output_dir=output_dir,
            token=github_token,
            api_base_url=api_base_url,
            log_file=log_file,
        )
        for artifact_path in artifact_paths:
            artifact_total, artifact_failed, artifact_skipped, report_format = collect_test_counts_under(artifact_path)
            append_log(
                log_file,
                f"Artifact report counts ({report_format}): tests={artifact_total}, failed={artifact_failed}, skipped={artifact_skipped}",
            )
        if artifact_paths:
            total_tests, failed_tests, skipped_tests, _report_format = collect_test_counts_across_roots(artifact_paths)
            append_log(
                log_file,
                f"Artifact total counts: tests={total_tests}, failed={failed_tests}, skipped={skipped_tests}",
            )
    result = WorkflowResult(
        type=executor.type,
        repository=executor.name,
        repo_url=executor.github_url,
        branch=executor.branch,
        workflow=workflow_label,
        status=status,
        exit_code=0 if status == STATUS_PASSED else 1,
        duration_seconds=actual_duration_seconds,
        commands=[],
        executed_commands=[],
        output_dir=str(output_dir),
        log_file=str(log_file),
        copied_result_paths=[str(path) for path in artifact_paths],
        total_tests=total_tests,
        failed_tests=failed_tests,
        skipped_tests=skipped_tests,
        started_at=actual_started_at,
        finished_at=actual_finished_at,
        details=details,
    )
    write_json(output_dir / "result.json", asdict(result))
    return result


def is_reusable_only_workflow(workflow_file: Path) -> bool:
    text = workflow_file.read_text(encoding="utf-8").lower()
    if "workflow_call:" not in text:
        return False
    trigger_markers = (
        "push:",
        "pull_request:",
        "workflow_dispatch:",
        "repository_dispatch:",
        "schedule:",
    )
    return not any(marker in text for marker in trigger_markers)


def is_reusable_only_workflow_text(text: str) -> bool:
    normalized = text.lower()
    if "workflow_call:" not in normalized:
        return False
    trigger_markers = (
        "push:",
        "pull_request:",
        "workflow_dispatch:",
        "repository_dispatch:",
        "schedule:",
    )
    return not any(marker in normalized for marker in trigger_markers)


def parse_inline_value(line: str) -> str:
    _, _, remainder = line.partition(":")
    return strip_yaml_value(remainder)


def collect_yaml_block(lines: list[str], start_index: int, block_indent: int) -> tuple[str, int]:
    collected: list[str] = []
    index = start_index
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        current_indent = len(line) - len(line.lstrip(" "))
        if stripped and current_indent < block_indent:
            break
        collected.append("" if not stripped else line[block_indent:])
        index += 1
    return "\n".join(collected).strip(), index


def normalize_shellish_command(command: str) -> str:
    normalized = command.strip()
    normalized = normalized.removeprefix("sudo ").strip()
    normalized = normalized.removeprefix("time ").strip()
    return normalized


def has_unresolved_github_expression(command: str) -> bool:
    return "${{" in command and "}}" in command


def unresolved_github_expression_keys(command: str) -> list[str]:
    return re.findall(r"\$\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}", command)


def has_only_matrix_github_expressions(command: str) -> bool:
    keys = unresolved_github_expression_keys(command)
    return bool(keys) and all(key.startswith("matrix.") for key in keys)


def strip_yaml_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_scalar(value: str) -> str:
    value = strip_yaml_value(value)
    if value.lower() == "true":
        return "true"
    if value.lower() == "false":
        return "false"
    return value


def is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_linux_host() -> bool:
    return os.name != "nt" and "linux" in os.uname().sysname.lower() if hasattr(os, "uname") else False


def should_skip_matrix_include(matrix: dict[str, str]) -> bool:
    matrix_name = (matrix.get("name") or "").strip().lower()
    test_name = (matrix.get("testName") or "").strip().lower()
    if not is_linux_host() and (
        matrix_name == "docker"
        or "testcontainer" in test_name
        or "container" in test_name
    ):
        return True

    return False


def parse_matrix_includes(lines: list[str]) -> list[dict[str, str]]:
    includes: list[dict[str, str]] = []
    in_strategy = False
    in_matrix = False
    in_include = False
    strategy_indent = -1
    matrix_indent = -1
    include_indent = -1
    current: dict[str, str] | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        if stripped.startswith("strategy:"):
            in_strategy = True
            in_matrix = False
            in_include = False
            strategy_indent = indent
            continue

        if in_strategy and indent <= strategy_indent and not stripped.startswith("strategy:"):
            break

        if in_strategy and stripped.startswith("matrix:"):
            in_matrix = True
            matrix_indent = indent
            continue

        if in_matrix and indent <= matrix_indent and not stripped.startswith("matrix:"):
            in_matrix = False
            in_include = False

        if in_matrix and stripped.startswith("include:"):
            in_include = True
            include_indent = indent
            continue

        if in_include and indent <= include_indent and not stripped.startswith("- "):
            break

        if not in_include:
            continue

        if stripped.startswith("- "):
            if current:
                includes.append(current)
            current = {}
            item = stripped[2:].strip()
            if ":" in item:
                key, _, value = item.partition(":")
                current[key.strip()] = parse_scalar(value)
            continue

        if current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = parse_scalar(value)

    if current:
        includes.append(current)

    return includes


def expand_matrix_expressions(command: str, matrix_includes: list[dict[str, str]]) -> list[tuple[str, dict[str, str]]]:
    matrix_keys = re.findall(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}", command)
    if not matrix_keys:
        return [(command, {})]
    if not matrix_includes:
        return [(command, {})]

    expanded: list[tuple[str, dict[str, str]]] = []
    for matrix in matrix_includes:
        if should_skip_matrix_include(matrix):
            continue
        expanded_command = command
        missing_value = False
        for key in matrix_keys:
            if key not in matrix:
                missing_value = True
                break
            expanded_command = re.sub(
                r"\$\{\{\s*matrix\." + re.escape(key) + r"\s*\}\}",
                matrix[key],
                expanded_command,
            )
        if not missing_value:
            expanded.append((expanded_command, matrix))
    return expanded or [(command, {})]


def substitute_input_expressions(text: str, inputs: dict[str, str]) -> str:
    def replacer(match: re.Match[str]) -> str:
        key = match.group(1)
        return inputs.get(key, "")

    return INPUT_EXPRESSION_RE.sub(replacer, text)


def parse_reusable_workflow_calls(lines: list[str]) -> list[ReusableWorkflowCall]:
    calls: list[ReusableWorkflowCall] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        match = REUSABLE_WORKFLOW_USES_RE.match(line)
        if not match:
            index += 1
            continue

        uses_indent = len(line) - len(line.lstrip(" "))
        workflow_file = f".github/workflows/{match.group(1)}"
        inputs: dict[str, str] = {}
        index += 1
        saw_with = False
        with_indent = -1

        while index < len(lines):
            current_line = lines[index]
            stripped = current_line.strip()
            current_indent = len(current_line) - len(current_line.lstrip(" "))
            if stripped and current_indent < uses_indent:
                break

            if stripped.startswith("with:"):
                saw_with = True
                with_indent = current_indent
                index += 1
                continue

            if saw_with:
                if stripped and current_indent <= with_indent:
                    saw_with = False
                    continue
                if ":" in stripped:
                    key, _, value = stripped.partition(":")
                    inputs[key.strip()] = parse_scalar(value)

            index += 1

        calls.append(ReusableWorkflowCall(workflow_path=workflow_file, inputs=inputs))

    return calls


def should_consider_workflow_for_execution_text(text: str, workflow_label: str) -> bool:
    lines = text.splitlines()
    if Path(workflow_label).name.lower() in SKIPPED_WORKFLOW_FILE_NAMES:
        return False
    if is_reusable_only_workflow_text(text):
        return False
    synthetic_repo_dir = Path.cwd()
    synthetic_workflow_file = synthetic_repo_dir / workflow_label
    commands = extract_workflow_commands_from_lines(
        workflow_file=synthetic_workflow_file,
        repo_dir=synthetic_repo_dir,
        lines=lines,
        input_values={},
        allow_matrix_expressions=True,
    )
    if any(
        is_test_command(command.command, allow_matrix_expressions=True)
        or is_playwright_test_wrapper_command(command.command, workflow_label)
        for command in commands
    ):
        return True
    return bool(parse_reusable_workflow_calls(lines))


def should_consider_workflow_for_execution(workflow_file: Path, repo_dir: Path) -> bool:
    relative_workflow_path = str(workflow_file.resolve().relative_to(repo_dir.resolve())).replace("\\", "/")
    return should_consider_workflow_for_execution_text(
        workflow_file.read_text(encoding="utf-8"),
        relative_workflow_path,
    )


def is_test_command(command: str, allow_matrix_expressions: bool = False) -> bool:
    lower = f" {command.lower()} "
    stripped_lower = lower.strip()
    if any(stripped_lower.startswith(prefix) for prefix in SKIP_COMMAND_PREFIXES):
        return False
    if has_unresolved_github_expression(command):
        if not allow_matrix_expressions or not has_only_matrix_github_expressions(command):
            return False
    if " -x test" in lower:
        return False
    if "gradlew" in lower or "gradle " in lower or "mvn" in lower or "pytest" in lower or "go test" in lower or "dotnet test" in lower:
        return any(keyword in lower for keyword in TEST_KEYWORDS)
    if "npm " in lower or "pnpm " in lower or "yarn " in lower:
        return " test" in lower or " e2e" in lower
    if "playwright test" in lower:
        return True
    return False


def is_playwright_setup_command(command: str, workflow_file_path: str) -> bool:
    lower = command.lower().strip()
    workflow_lower = workflow_file_path.lower()
    if "playwright" in workflow_lower:
        if lower.startswith("npm ci"):
            return True
        if lower.startswith("npm install"):
            return True
        if lower.startswith("pnpm install"):
            return True
        if lower.startswith("yarn install"):
            return True
    if "playwright install" in lower:
        return True
    return False


def is_playwright_test_wrapper_command(command: str, workflow_file_path: str) -> bool:
    lower = command.lower().strip()
    workflow_lower = workflow_file_path.lower()
    if "playwright" not in workflow_lower:
        return False
    return (
        "scripts/github/run-playwright-group.sh" in lower
        or "scripts\\github\\run-playwright-group.sh" in lower
    )


def is_runnable_workflow_command(
    command: str,
    workflow_file_path: str,
    allow_matrix_expressions: bool = False,
) -> bool:
    return (
        is_test_command(command, allow_matrix_expressions=allow_matrix_expressions)
        or is_playwright_setup_command(command, workflow_file_path)
        or is_playwright_test_wrapper_command(command, workflow_file_path)
    )


def split_logical_commands(run_block: str) -> list[str]:
    logical_lines: list[str] = []
    current = ""
    for raw_line in run_block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}".strip() if current else line
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        logical_lines.append(current)
        current = ""
    if current:
        logical_lines.append(current)
    return logical_lines


def build_discovered_workflow_commands(
    workflow_file: Path,
    repo_dir: Path,
    step_name: str,
    working_directory: str,
    run_block: str,
    matrix_includes: list[dict[str, str]],
    input_values: dict[str, str],
    allow_matrix_expressions: bool = False,
) -> list[WorkflowCommand]:
    relative_workflow_path = str(workflow_file.resolve().relative_to(repo_dir.resolve())).replace("\\", "/")
    commands: list[WorkflowCommand] = []
    for line in split_logical_commands(run_block):
        line = substitute_input_expressions(line, input_values)
        for expanded_line, matrix_value in expand_matrix_expressions(line, matrix_includes):
            normalized = normalize_shellish_command(expanded_line)
            if not normalized or not is_runnable_workflow_command(
                normalized,
                relative_workflow_path,
                allow_matrix_expressions=allow_matrix_expressions,
            ):
                continue
            commands.append(
                WorkflowCommand(
                    workflow_file=relative_workflow_path,
                    step_name=step_name,
                    command=normalized,
                    working_directory=working_directory or ".",
                    needs_cli_install=is_truthy(matrix_value.get("needsCliInstall")),
                )
                )
    return commands


def extract_workflow_commands_from_lines(
    workflow_file: Path,
    repo_dir: Path,
    lines: list[str],
    input_values: dict[str, str],
    allow_matrix_expressions: bool = False,
) -> list[WorkflowCommand]:
    matrix_includes = parse_matrix_includes(lines)
    commands: list[WorkflowCommand] = []
    current_step_name = ""
    current_workdir = "."
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        step_match = re.match(r"^(\s*)-\s+(.*)$", line)

        if step_match:
            current_step_name = ""
            current_workdir = "."
            remainder = step_match.group(2)
            if remainder.startswith("name:"):
                current_step_name = parse_inline_value(remainder)
            elif remainder.startswith("working-directory:"):
                current_workdir = parse_inline_value(remainder)
            elif remainder.startswith("run:"):
                raw_value = remainder[4:].strip()
                if raw_value in {"|", "|-", "|+", ">", ">-", ">+"}:
                    block_indent = len(line) - len(line.lstrip(" ")) + 2
                    run_block, index = collect_yaml_block(lines, index + 1, block_indent)
                else:
                    run_block = strip_yaml_value(raw_value)
                    index += 1
                commands.extend(
                    build_discovered_workflow_commands(
                        workflow_file=workflow_file,
                        repo_dir=repo_dir,
                        step_name=substitute_input_expressions(current_step_name or "unnamed step", input_values),
                        working_directory=substitute_input_expressions(current_workdir, input_values),
                        run_block=substitute_input_expressions(run_block, input_values),
                        matrix_includes=matrix_includes,
                        input_values=input_values,
                        allow_matrix_expressions=allow_matrix_expressions,
                    )
                )
                continue
            index += 1
            continue

        if stripped.startswith("name:"):
            current_step_name = parse_inline_value(line)
        elif stripped.startswith("working-directory:"):
            current_workdir = parse_inline_value(line)
        elif stripped.startswith("run:"):
            raw_value = stripped[4:].strip()
            if raw_value in {"|", "|-", "|+", ">", ">-", ">+"}:
                block_indent = len(line) - len(line.lstrip(" ")) + 2
                run_block, index = collect_yaml_block(lines, index + 1, block_indent)
            else:
                run_block = strip_yaml_value(raw_value)
                index += 1
            commands.extend(
                build_discovered_workflow_commands(
                    workflow_file=workflow_file,
                    repo_dir=repo_dir,
                    step_name=substitute_input_expressions(current_step_name or "unnamed step", input_values),
                    working_directory=substitute_input_expressions(current_workdir, input_values),
                    run_block=substitute_input_expressions(run_block, input_values),
                    matrix_includes=matrix_includes,
                    input_values=input_values,
                    allow_matrix_expressions=allow_matrix_expressions,
                )
            )
            continue

        index += 1

    return commands


def extract_workflow_commands_recursive(
    workflow_file: Path,
    repo_dir: Path,
    input_values: dict[str, str],
    visited: set[Path],
) -> list[WorkflowCommand]:
    resolved_workflow = workflow_file.resolve()
    if resolved_workflow in visited:
        return []
    visited.add(resolved_workflow)

    lines = resolved_workflow.read_text(encoding="utf-8").splitlines()
    commands = extract_workflow_commands_from_lines(
        workflow_file=resolved_workflow,
        repo_dir=repo_dir,
        lines=lines,
        input_values=input_values,
    )

    for call in parse_reusable_workflow_calls(lines):
        nested_inputs = {
            key: substitute_input_expressions(value, input_values)
            for key, value in call.inputs.items()
        }
        nested_workflow = (repo_dir / call.workflow_path).resolve()
        if not nested_workflow.exists():
            continue
        commands.extend(
            extract_workflow_commands_recursive(
                workflow_file=nested_workflow,
                repo_dir=repo_dir,
                input_values=nested_inputs,
                visited=visited,
            )
        )

    return commands


def extract_workflow_commands(workflow_file: Path, repo_dir: Path) -> list[WorkflowCommand]:
    lines = workflow_file.read_text(encoding="utf-8").splitlines()
    commands: list[WorkflowCommand] = extract_workflow_commands_from_lines(
        workflow_file=workflow_file,
        repo_dir=repo_dir,
        lines=lines,
        input_values={},
    )

    for call in parse_reusable_workflow_calls(lines):
        nested_workflow = (repo_dir / call.workflow_path).resolve()
        if not nested_workflow.exists():
            continue
        commands.extend(
            extract_workflow_commands_recursive(
                workflow_file=nested_workflow,
                repo_dir=repo_dir,
                input_values=call.inputs,
                visited={workflow_file.resolve()},
            )
        )

    deduped: list[WorkflowCommand] = []
    seen: set[tuple[str, str, str, str]] = set()
    for command in commands:
        key = (command.workflow_file, command.step_name, command.command, command.working_directory)
        if key not in seen:
            seen.add(key)
            deduped.append(command)
    return deduped


def extract_run_commands(workflow_file: Path) -> list[str]:
    repo_dir = workflow_file.parents[2] if len(workflow_file.parents) > 2 else workflow_file.parent
    return [command.command for command in extract_workflow_commands(workflow_file, repo_dir)]




def is_playwright_executor(executor: TestExecutor) -> bool:
    return "playwright" in executor.type.lower() or "playwright" in executor.name.lower()


def is_windows_enterprise_configuration() -> bool:
    configuration = os.environ.get("ENTERPRISE_CONFIGURATION", "").strip().lower()
    return configuration.startswith("windows")


def should_skip_playwright_executor(executor: TestExecutor) -> bool:
    return is_playwright_executor(executor) and is_windows_enterprise_configuration()


def is_sample_project_executor(executor: TestExecutor) -> bool:
    return executor.type.lower() == "sample-project"


def cleanup_playwright_containers(log_file: Path, phase: str) -> None:
    log_progress(f"     docker cleanup ({phase}): {', '.join(PLAYWRIGHT_CONTAINER_NAMES)}")
    run_command(
        ["docker", "rm", "-f", *PLAYWRIGHT_CONTAINER_NAMES],
        cwd=None,
        env=os.environ.copy(),
        log_file=log_file,
    )


def should_cleanup_shared_containers(executor: TestExecutor) -> bool:
    return is_sample_project_executor(executor) or is_playwright_executor(executor)


def is_playwright_jar_mode(cli_setup_config: CliSetupConfig) -> bool:
    return bool((cli_setup_config.jar_url or "").strip() or (cli_setup_config.jar_path or "").strip())


def resolve_playwright_compose_file(repo_dir: Path, jar_mode: bool) -> Path | None:
    preferred_names: list[str] = []
    if jar_mode:
        preferred_names.extend(["docker-compose-jar.yaml", "docker-compose-jar.yml"])
    preferred_names.extend(["docker-compose.yaml", "docker-compose.yml"])

    search_roots = [repo_dir]
    nested_demo = repo_dir / "specmatic-studio-demo"
    if nested_demo.exists():
        search_roots.insert(0, nested_demo)

    for root in search_roots:
        for file_name in preferred_names:
            candidate = root / file_name
            if candidate.exists():
                return candidate

    # Fallback: recursive search by preferred names anywhere in repo.
    for file_name in preferred_names:
        matches = sorted(path for path in repo_dir.rglob(file_name) if path.is_file())
        if matches:
            return matches[0]

    return None


def is_http_ready(url: str, timeout_seconds: float = 2.0) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return 200 <= int(getattr(response, "status", 0)) < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_playwright_support_services(timeout_seconds: int = 120) -> tuple[bool, str]:
    deadline = time.time() + timeout_seconds
    pending = set(PLAYWRIGHT_SERVICE_HEALTH_URLS.keys())
    while time.time() < deadline:
        for service in list(pending):
            if is_http_ready(PLAYWRIGHT_SERVICE_HEALTH_URLS[service]):
                pending.remove(service)
        if not pending:
            return True, "support services are healthy"
        time.sleep(2)
    return False, f"support services did not become healthy: {', '.join(sorted(pending))}"


def start_playwright_support_runtime(
    executor: TestExecutor,
    repo_dir: Path,
    outputs_dir: Path,
    jar_mode: bool,
    cli_setup_config: CliSetupConfig,
    dry_run: bool,
) -> tuple[bool, str]:
    runtime_dir = outputs_dir / executor.type / executor.name / "_runtime"
    runtime_log = runtime_dir / "run.log"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_log.write_text("", encoding="utf-8")

    compose_file = resolve_playwright_compose_file(repo_dir, jar_mode=jar_mode)
    if compose_file is None:
        return False, "no docker-compose file found for Playwright runtime"

    services = PLAYWRIGHT_CONTAINER_NAMES
    compose_file = compose_file.resolve()
    compose_cwd = compose_file.parent
    compose_files = [str(compose_file)]
    if jar_mode:
        ok, setup_details, jar_path = ensure_enterprise_jar_available(
            cli_setup_config,
            log_file=runtime_log,
            dry_run=dry_run,
        )
        if not ok or jar_path is None:
            return False, f"failed to prepare studio jar for compose runtime ({setup_details})"

        override_file = runtime_dir / "docker-compose.jar-override.yml"
        specs_dir = (compose_cwd / "specs").resolve()
        override_file.write_text(
            "\n".join(
                [
                    "services:",
                    "  studio:",
                    "    image: eclipse-temurin:17-jdk",
                    "    working_dir: /usr/src/app",
                    "    command: [\"java\", \"-jar\", \"/app/specmatic.jar\", \"studio\"]",
                    "    volumes:",
                    f"      - {specs_dir.as_posix()}:/usr/src/app",
                    f"      - {jar_path.resolve().as_posix()}:/app/specmatic.jar:ro",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        compose_files.append(str(override_file.resolve()))

    command = ["docker", "compose"]
    for compose_path in compose_files:
        command.extend(["-f", compose_path])
    command.extend(["up", "-d", *services])
    exit_code = run_command(command, cwd=compose_cwd, env=os.environ.copy(), log_file=runtime_log)
    if exit_code != 0:
        return False, f"docker compose up failed using {compose_file.name}"

    ok, details = wait_for_playwright_support_services()
    if not ok:
        return False, details
    return True, f"started playwright support runtime using {compose_file.name}"


def stop_playwright_support_runtime(executor: TestExecutor, repo_dir: Path, outputs_dir: Path, jar_mode: bool) -> None:
    runtime_dir = outputs_dir / executor.type / executor.name / "_runtime"
    runtime_log = runtime_dir / "run.log"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if not runtime_log.exists():
        runtime_log.write_text("", encoding="utf-8")
    compose_file = resolve_playwright_compose_file(repo_dir, jar_mode=jar_mode)
    if compose_file is None:
        return
    compose_file = compose_file.resolve()
    compose_cwd = compose_file.parent
    compose_files = [str(compose_file)]
    if jar_mode:
        override_file = runtime_dir / "docker-compose.jar-override.yml"
        if override_file.exists():
            compose_files.append(str(override_file.resolve()))

    command = ["docker", "compose"]
    for compose_path in compose_files:
        command.extend(["-f", compose_path])
    command.extend(["down", "--remove-orphans"])
    run_command(
        command,
        cwd=compose_cwd,
        env=os.environ.copy(),
        log_file=runtime_log,
    )


def read_log_tail(log_file: Path, max_bytes: int = 200_000) -> str:
    if not log_file.exists():
        return ""
    with log_file.open("rb") as stream:
        try:
            stream.seek(0, os.SEEK_END)
            file_size = stream.tell()
            stream.seek(max(file_size - max_bytes, 0), os.SEEK_SET)
            data = stream.read()
        except OSError:
            return ""
    return data.decode("utf-8", errors="ignore")


def enrich_failure_details_from_log(status: str, details: str, log_file: Path) -> str:
    if status not in {STATUS_COMMAND_FAILED, STATUS_FAILED}:
        return details

    tail = read_log_tail(log_file)
    if "PortInUseException" in tail or "Address already in use" in tail:
        return f"{details}; local port conflict detected (example: 8080/8090 already in use)"
    return details


def select_runnable_commands(commands: list[WorkflowCommand]) -> list[WorkflowCommand]:
    test_commands = [command for command in commands if is_test_command(command.command)]
    if not test_commands:
        return []
    return commands


def tokenize_command(command: str) -> list[str]:
    return shlex.split(command, posix=os.name != "nt")


def normalize_command_for_os(command: list[str], repo_dir: Path) -> list[str]:
    if not command:
        return command

    normalized_args: list[str] = []
    for arg in command:
        if arg.startswith("--tests="):
            value = arg.split("=", 1)[1]
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                arg = f"--tests={value[1:-1]}"
        normalized_args.append(arg)

    command = normalized_args
    if os.name == "nt" and command:
        launcher = command[0].lower()
        if launcher in {"npm", "npx", "pnpm", "yarn"}:
            cmd_path = shutil.which(f"{launcher}.cmd")
            if cmd_path:
                command = [cmd_path, *command[1:]]

    if (
        os.name == "nt"
        and len(command) >= 3
        and Path(command[0]).stem.lower() == "npx"
        and command[1] == "playwright"
        and command[2] == "install"
    ):
        command = [arg for arg in command if arg != "--with-deps"]

    first = command[0]
    normalized = first.replace("/", "\\") if os.name == "nt" else first

    if os.name == "nt" and normalized in {".\\gradlew", "gradlew", "./gradlew"}:
        gradle_bat = repo_dir / "gradlew.bat"
        if gradle_bat.exists():
            return [str(gradle_bat.resolve()), *command[1:]]

    if os.name != "nt" and normalized in {"./gradlew", "gradlew"}:
        gradlew = repo_dir / "gradlew"
        if gradlew.exists():
            gradlew.chmod(0o755)
            return [str(gradlew.resolve()), *command[1:]]

    return [normalized, *command[1:]]


def is_gradle_invocation(command: list[str]) -> bool:
    if not command:
        return False
    first_name = Path(command[0]).name.lower()
    if first_name in {"gradlew", "gradlew.bat"}:
        return True
    return first_name in {"gradle", "gradle.bat"}


def apply_gradle_version_overrides(
    command: list[str],
    specmatic_version: str,
    enterprise_version: str,
    snapshot_repo_url: str = "",
) -> list[str]:
    if not is_gradle_invocation(command):
        return command

    overridden = list(command)
    if specmatic_version and not any(arg.startswith("-PspecmaticVersion=") for arg in overridden):
        overridden.append(f"-PspecmaticVersion={specmatic_version}")
    if enterprise_version:
        if not any(arg.startswith("-PspecmaticEnterpriseVersion=") for arg in overridden):
            overridden.append(f"-PspecmaticEnterpriseVersion={enterprise_version}")
        if not any(arg.startswith("-PenterpriseVersion=") for arg in overridden):
            overridden.append(f"-PenterpriseVersion={enterprise_version}")
    if snapshot_repo_url and not any(arg.startswith("-PsnapshotRepoUrl=") for arg in overridden):
        overridden.append(f"-PsnapshotRepoUrl={snapshot_repo_url}")
    return overridden


def parse_additional_env_variables(entries: list[str] | None) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for entry in entries or []:
        raw = entry.strip()
        if not raw or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        env_map[key] = value
    return env_map


def build_command_env(
    repo_dir: Path,
    output_dir: Path,
    workflow_file: str,
    executor: TestExecutor,
    specmatic_version: str = "",
    enterprise_version: str = "",
    enterprise_docker_image: str = "",
    specmatic_jar_url: str = "",
    specmatic_jar_path: str = "",
) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("CI", "true")
    env["GITHUB_WORKSPACE"] = str(repo_dir.resolve())
    env["ORCHESTRATOR_WORKFLOW_FILE"] = workflow_file
    env["ORCHESTRATOR_OUTPUT_DIR"] = str(output_dir.resolve())
    effective_specmatic_version = "" if is_sample_project_executor(executor) else specmatic_version
    if effective_specmatic_version:
        env["ORG_GRADLE_PROJECT_specmaticVersion"] = effective_specmatic_version
    if enterprise_version:
        env["ORG_GRADLE_PROJECT_specmaticEnterpriseVersion"] = enterprise_version
        env["ORG_GRADLE_PROJECT_enterpriseVersion"] = enterprise_version
    if specmatic_jar_url and not is_playwright_executor(executor):
        env["SPECMATIC_JAR_URL"] = specmatic_jar_url
        env["SPECMATIC_STUDIO_JAR_URL"] = specmatic_jar_url
    if specmatic_jar_path and not is_playwright_executor(executor):
        env["SPECMATIC_JAR_PATH"] = specmatic_jar_path
    if enterprise_docker_image:
        env["ENTERPRISE_DOCKER_IMAGE"] = enterprise_docker_image
        env["SPECMATIC_STUDIO_DOCKER_IMAGE"] = enterprise_docker_image

    additional_env_map = parse_additional_env_variables(executor.additional_env_variables)
    if additional_env_map:
        log_progress("    Additional parameters passed:")
        for key in sorted(additional_env_map.keys()):
            value = additional_env_map[key]
            env[key] = value
            log_progress(f"      - {key}={value}")
    else:
        log_progress("    No Additional parameters passed.")

    if is_playwright_executor(executor):
        env["SPECMATIC_TEST_ORCHESTRATOR"] = "true"
        if not env.get("APPLITOOLS_API_KEY"):
            env["ENABLE_VISUAL"] = "false"
        env["SPECMATIC_STUDIO_JAR_URL"] = ""
        env["SPECMATIC_JAR_URL"] = ""
        env["SPECMATIC_JAR_PATH"] = ""
    return env


def cli_jar_path() -> Path:
    return Path.home() / ".specmatic" / "specmatic-enterprise.jar"


def resolve_enterprise_jar_source(config: CliSetupConfig) -> Path | None:
    if config.jar_path:
        return Path(config.jar_path).expanduser().resolve()
    if config.jar_url:
        return None
    target_jar = cli_jar_path()
    if target_jar.exists():
        return target_jar
    return None


def ensure_enterprise_jar_available(config: CliSetupConfig, log_file: Path, dry_run: bool) -> tuple[bool, str, Path | None]:
    target_jar = cli_jar_path()
    target_jar.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"\n[enterprise-jar] dry-run for {target_jar}\n")
        return True, f"dry-run: Enterprise jar setup skipped for {target_jar}", target_jar

    source = resolve_enterprise_jar_source(config)
    if source:
        if not source.exists():
            return False, f"specmatic jar path does not exist: {source}", None
        if not zipfile.is_zipfile(source):
            return False, f"specmatic jar path is not a valid jar: {source}", None
        if source != target_jar.resolve():
            shutil.copy2(source, target_jar)
            return True, f"copied Specmatic jar to {target_jar}", target_jar
        return True, f"Specmatic jar already present at {target_jar}", target_jar

    if config.jar_url:
        try:
            urllib.request.urlretrieve(config.jar_url, target_jar)
        except (urllib.error.URLError, OSError) as exc:
            return False, f"failed to download Specmatic jar from URL: {exc}", None
        if not zipfile.is_zipfile(target_jar):
            return False, f"downloaded Specmatic jar is not a valid jar: {target_jar}", None
        return True, f"downloaded Specmatic jar to {target_jar}", target_jar

    return False, "Specmatic Enterprise jar not found. Provide --specmatic-jar-path, --specmatic-jar-url, or install ~/.specmatic/specmatic-enterprise.jar.", None


def prepare_cli_dependency(config: CliSetupConfig, log_file: Path, dry_run: bool, enterprise_version: str = "") -> tuple[bool, str]:
    ok, details, _ = ensure_enterprise_jar_available(config, log_file=log_file, dry_run=dry_run)
    if ok:
        return True, details

    if config.allow_installer and os.name != "nt":
        installer_args = f" -- --version {shlex.quote(enterprise_version)}" if enterprise_version else ""
        command = [
            "bash",
            "-lc",
            f"curl -fsSL https://docs.specmatic.io/install-specmatic-enterprise.sh | bash -s{installer_args}",
        ]
        exit_code = run_command(command, cwd=None, env=os.environ.copy(), log_file=log_file)
        target_jar = cli_jar_path()
        if exit_code == 0 and target_jar.exists():
            return True, f"installed Specmatic jar to {target_jar}"
        return False, "CLI installer ran but Specmatic jar was not found"

    return False, details


def write_enterprise_maven_repo(repo_dir: Path, jar_path: Path, enterprise_version: str) -> str:
    artifact_dir = repo_dir / "io" / "specmatic" / "enterprise" / "executable" / enterprise_version
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_base = f"executable-{enterprise_version}"
    shutil.copy2(jar_path, artifact_dir / f"{artifact_base}.jar")
    (artifact_dir / f"{artifact_base}.pom").write_text(
        "\n".join(
            [
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<project xmlns="http://maven.apache.org/POM/4.0.0"',
                '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
                '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">',
                "  <modelVersion>4.0.0</modelVersion>",
                "  <groupId>io.specmatic.enterprise</groupId>",
                "  <artifactId>executable</artifactId>",
                f"  <version>{enterprise_version}</version>",
                "</project>",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return repo_dir.resolve().as_uri()


def has_explicit_enterprise_jar_source(config: CliSetupConfig) -> bool:
    return bool(config.jar_path or config.jar_url)


def can_prepare_enterprise_maven_repo(config: CliSetupConfig) -> bool:
    return has_explicit_enterprise_jar_source(config) or config.allow_installer or cli_jar_path().exists()


def copy_result_paths(repo_dir: Path, output_dir: Path, patterns: list[str]) -> list[str]:
    copied: list[str] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    def normalize_windows_path(path: Path) -> str:
        resolved = str(path.resolve())
        if os.name == "nt" and not resolved.startswith("\\\\?\\"):
            return "\\\\?\\" + resolved
        return resolved

    def copy_directory_resilient(source_dir: Path, destination_dir: Path) -> int:
        copied_files = 0
        for item in source_dir.rglob("*"):
            if not item.is_file():
                continue
            try:
                relative = item.relative_to(source_dir)
            except ValueError:
                continue
            destination_file = destination_dir / relative
            destination_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(normalize_windows_path(item), normalize_windows_path(destination_file))
                copied_files += 1
            except (FileNotFoundError, OSError):
                # Files under test-results/playwright-report can disappear between scan and copy.
                continue
        return copied_files

    for pattern in patterns:
        for source in repo_dir.glob(pattern):
            if not source.exists():
                continue

            destination = output_dir / source.relative_to(repo_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                if destination.exists():
                    remove_tree(destination)
                try:
                    shutil.copytree(
                        normalize_windows_path(source),
                        normalize_windows_path(destination),
                        dirs_exist_ok=False,
                    )
                except (shutil.Error, FileNotFoundError, OSError):
                    destination.mkdir(parents=True, exist_ok=True)
                    copy_directory_resilient(source, destination)
            elif source.is_file():
                try:
                    shutil.copy2(normalize_windows_path(source), normalize_windows_path(destination))
                except (FileNotFoundError, OSError):
                    continue
            copied.append(str(source.relative_to(repo_dir)).replace("\\", "/"))

    return copied


def clean_result_paths(repo_dir: Path, patterns: list[str]) -> None:
    for pattern in patterns:
        for source in repo_dir.glob(pattern):
            if source.is_dir():
                remove_tree(source)
            elif source.is_file():
                source.unlink()


def read_int_attribute(element: ET.Element, attribute: str) -> int:
    raw_value = element.attrib.get(attribute, "0")
    try:
        return int(float(raw_value))
    except ValueError:
        return 0


def collect_junit_counts_from_element(element: ET.Element) -> tuple[int, int, int]:
    total = read_int_attribute(element, "tests")
    failed = read_int_attribute(element, "failures") + read_int_attribute(element, "errors")
    skipped = read_int_attribute(element, "skipped")
    return total, failed, skipped


def collect_junit_counts_from_xml(xml_file: Path) -> tuple[int, int, int]:
    try:
        root = ET.parse(xml_file).getroot()
    except ET.ParseError:
        return 0, 0, 0

    if root.tag == "testsuite":
        return collect_junit_counts_from_element(root)
    if root.tag != "testsuites":
        return 0, 0, 0

    total = 0
    failed = 0
    skipped = 0
    for child in root.findall("testsuite"):
        child_total, child_failed, child_skipped = collect_junit_counts_from_element(child)
        total += child_total
        failed += child_failed
        skipped += child_skipped
    return total, failed, skipped


def collect_junit_counts(repo_dir: Path) -> tuple[int, int, int]:
    candidate_dirs = [
        repo_dir / "build" / "test-results",
        repo_dir / "test-results",
        repo_dir / "junit-results",
        repo_dir / "playwright-report",
    ]
    candidate_dirs.extend(path for path in repo_dir.glob("playwright-report-*") if path.is_dir())

    total = 0
    failed = 0
    skipped = 0
    seen: set[Path] = set()
    for candidate_dir in candidate_dirs:
        if not candidate_dir.exists():
            continue
        for xml_file in candidate_dir.rglob("*.xml"):
            resolved = xml_file.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            file_total, file_failed, file_skipped = collect_junit_counts_from_xml(xml_file)
            total += file_total
            failed += file_failed
            skipped += file_skipped

    return total, failed, skipped


def collect_junit_counts_under(root_dir: Path) -> tuple[int, int, int]:
    if not root_dir.exists():
        return 0, 0, 0
    total = 0
    failed = 0
    skipped = 0
    seen: set[Path] = set()
    for xml_file in root_dir.rglob("*.xml"):
        resolved = xml_file.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        file_total, file_failed, file_skipped = collect_junit_counts_from_xml(xml_file)
        total += file_total
        failed += file_failed
        skipped += file_skipped
    return total, failed, skipped


def collect_junit_counts_from_files(xml_files: list[Path]) -> tuple[int, int, int]:
    total = 0
    failed = 0
    skipped = 0
    seen: set[Path] = set()
    for xml_file in xml_files:
        resolved = xml_file.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        file_total, file_failed, file_skipped = collect_junit_counts_from_xml(xml_file)
        total += file_total
        failed += file_failed
        skipped += file_skipped
    return total, failed, skipped


def parse_playwright_json_summary(report_file: Path) -> tuple[int, int, int]:
    try:
        payload = json.loads(report_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0, 0
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return 0, 0, 0
    expected = to_int(stats.get("expected"))
    unexpected = to_int(stats.get("unexpected"))
    flaky = to_int(stats.get("flaky"))
    skipped = to_int(stats.get("skipped"))
    total = expected + unexpected + flaky + skipped
    return total, unexpected, skipped


def collect_ctrf_counts_under(root_dir: Path) -> tuple[int, int, int]:
    return collect_ctrf_counts_from_files(sorted(root_dir.rglob("*.json")))


def ctrf_test_identity(test: dict[str, Any], fallback_index: int) -> str:
    test_id = str(test.get("id") or "").strip()
    if test_id:
        return test_id
    suite = str(test.get("suite") or "").strip()
    name = str(test.get("name") or "").strip()
    return f"{suite}::{name}::{fallback_index}"


def ctrf_status_priority(status: str) -> int:
    normalized = status.strip().lower()
    if normalized in {"failed", "failure", "broken", "error"}:
        return 3
    if normalized in {"skipped", "pending", "other"}:
        return 2
    return 1


def collect_ctrf_counts_from_files(json_files: list[Path]) -> tuple[int, int, int]:
    total = 0
    failed = 0
    skipped = 0
    unique_status_by_test: dict[str, str] = {}
    saw_detailed_tests = False

    for json_file in sorted(json_files):
        try:
            payload = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        results = payload.get("results", {})
        tests = results.get("tests", [])
        if isinstance(tests, list) and tests:
            saw_detailed_tests = True
            for index, test in enumerate(tests):
                if not isinstance(test, dict):
                    continue
                identity = ctrf_test_identity(test, index)
                status = str(test.get("status") or "").strip().lower() or "passed"
                existing = unique_status_by_test.get(identity, "")
                if ctrf_status_priority(status) >= ctrf_status_priority(existing):
                    unique_status_by_test[identity] = status
            continue

        file_total, _passed, file_failed, file_skipped = parse_ctrf_summary(json_file)
        total += file_total
        failed += file_failed
        skipped += file_skipped

    if saw_detailed_tests:
        total += len(unique_status_by_test)
        failed += sum(1 for status in unique_status_by_test.values() if ctrf_status_priority(status) == 3)
        skipped += sum(1 for status in unique_status_by_test.values() if ctrf_status_priority(status) == 2)

    return total, failed, skipped


def collect_playwright_json_counts_under(root_dir: Path) -> tuple[int, int, int]:
    total = 0
    failed = 0
    skipped = 0
    for json_file in sorted(root_dir.rglob("*.json")):
        file_total, file_failed, file_skipped = parse_playwright_json_summary(json_file)
        total += file_total
        failed += file_failed
        skipped += file_skipped
    return total, failed, skipped


def collect_playwright_json_counts_from_files(json_files: list[Path]) -> tuple[int, int, int]:
    total = 0
    failed = 0
    skipped = 0
    for json_file in sorted(json_files):
        file_total, file_failed, file_skipped = parse_playwright_json_summary(json_file)
        total += file_total
        failed += file_failed
        skipped += file_skipped
    return total, failed, skipped


def collect_test_counts_under(root_dir: Path) -> tuple[int, int, int, str]:
    junit_total, junit_failed, junit_skipped = collect_junit_counts_under(root_dir)
    if junit_total:
        return junit_total, junit_failed, junit_skipped, "junit"

    ctrf_total, ctrf_failed, ctrf_skipped = collect_ctrf_counts_under(root_dir)
    if ctrf_total:
        return ctrf_total, ctrf_failed, ctrf_skipped, "ctrf"

    playwright_total, playwright_failed, playwright_skipped = collect_playwright_json_counts_under(root_dir)
    if playwright_total:
        return playwright_total, playwright_failed, playwright_skipped, "playwright-json"

    return 0, 0, 0, "none"


def collect_test_counts_across_roots(root_dirs: list[Path]) -> tuple[int, int, int, str]:
    xml_files: list[Path] = []
    json_files: list[Path] = []
    seen_xml: set[Path] = set()
    seen_json: set[Path] = set()

    for root_dir in root_dirs:
        if not root_dir.exists():
            continue
        for xml_file in root_dir.rglob("*.xml"):
            resolved = xml_file.resolve()
            if resolved in seen_xml:
                continue
            seen_xml.add(resolved)
            xml_files.append(xml_file)
        for json_file in root_dir.rglob("*.json"):
            resolved = json_file.resolve()
            if resolved in seen_json:
                continue
            seen_json.add(resolved)
            json_files.append(json_file)

    junit_total, junit_failed, junit_skipped = collect_junit_counts_from_files(xml_files)
    if junit_total:
        return junit_total, junit_failed, junit_skipped, "junit"

    ctrf_total, ctrf_failed, ctrf_skipped = collect_ctrf_counts_from_files(json_files)
    if ctrf_total:
        return ctrf_total, ctrf_failed, ctrf_skipped, "ctrf"

    playwright_total, playwright_failed, playwright_skipped = collect_playwright_json_counts_from_files(json_files)
    if playwright_total:
        return playwright_total, playwright_failed, playwright_skipped, "playwright-json"

    return 0, 0, 0, "none"


def classify_final_status(status: str, details: str, total_tests: int, failed_tests: int) -> tuple[str, str]:
    if status == STATUS_COMMAND_FAILED and total_tests > 0 and failed_tests > 0:
        return STATUS_FAILED, "test failures detected"
    return status, details






def synthetic_result(
    executor: TestExecutor,
    outputs_dir: Path,
    workflow: str,
    status: str,
    details: str,
    exit_code: int,
) -> WorkflowResult:
    output_dir = outputs_dir / executor.type / executor.name / workflow
    log_file = output_dir / "run.log"
    output_dir.mkdir(parents=True, exist_ok=True)
    result = WorkflowResult(
        type=executor.type,
        repository=executor.name,
        repo_url=executor.github_url,
        branch=executor.branch,
        workflow=workflow,
        status=status,
        exit_code=exit_code,
        duration_seconds=0,
        commands=[],
        executed_commands=[],
        output_dir=str(output_dir),
        log_file=str(log_file),
        copied_result_paths=[],
        total_tests=0,
        failed_tests=0,
        skipped_tests=0,
        started_at=utc_now(),
        finished_at=utc_now(),
        details=details,
    )
    log_file.write_text(
        "\n".join(
            [
                f"Status: {status}",
                f"Details: {details}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_json(output_dir / "result.json", asdict(result))
    return result


def profiled_result(executor: TestExecutor, outputs_dir: Path) -> WorkflowResult:
    profile = executor.result_profile or {}
    output_dir = outputs_dir / executor.type / executor.name / "_profile"
    log_file = output_dir / "run.log"
    output_dir.mkdir(parents=True, exist_ok=True)
    passed = bool(profile.get("passed", True))
    total = int(profile.get("total", profile.get("passed_count", 0) + profile.get("failed_count", 0)))
    failed = int(profile.get("failed_count", 0))
    skipped = int(profile.get("skipped_count", profile.get("skipped", 0)))
    delay = float(profile.get("delay_sec", 0) or 0)
    if delay > 0:
        time.sleep(delay)
    status = STATUS_PASSED if passed and failed == 0 else STATUS_FAILED
    details = f"orchestrator-tester synthetic profile: {profile.get('kind', 'default')}"
    log_file.write_text(
        "\n".join(
            [
                "Synthetic orchestrator-tester result profile",
                f"kind={profile.get('kind', 'default')}",
                f"passed={passed}",
                f"total={total}",
                f"failed={failed}",
                f"skipped={skipped}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    result = WorkflowResult(
        type=executor.type,
        repository=executor.name,
        repo_url=executor.github_url,
        branch=executor.branch,
        workflow="_profile",
        status=status,
        exit_code=0 if status == STATUS_PASSED else 1,
        duration_seconds=int(delay),
        commands=[],
        executed_commands=[],
        output_dir=str(output_dir),
        log_file=str(log_file),
        copied_result_paths=[],
        total_tests=total,
        failed_tests=failed,
        skipped_tests=skipped,
        started_at=utc_now(),
        finished_at=utc_now(),
        details=details,
    )
    write_json(output_dir / "result.json", asdict(result))
    return result


def run_executor(
    executor: TestExecutor,
    outputs_dir: Path,
    github_token: str,
    api_base_url: str,
    poll_seconds: int,
    timeout_seconds: int,
    specmatic_version: str = "",
    enterprise_version: str = "",
    enterprise_docker_image: str = "",
    jar_url: str = "",
    jar_path: str = "",
) -> list[WorkflowResult]:
    setup_results, dispatched = dispatch_parallel_executor_workflows(
        executor=executor,
        outputs_dir=outputs_dir,
        github_token=github_token,
        api_base_url=api_base_url,
        specmatic_version=specmatic_version,
        enterprise_version=enterprise_version,
        enterprise_docker_image=enterprise_docker_image,
        jar_url=jar_url,
        jar_path=jar_path,
    )
    return setup_results + wait_for_parallel_workflows(
        dispatched=dispatched,
        outputs_dir=outputs_dir,
        github_token=github_token,
        api_base_url=api_base_url,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )


def parallel_executor_setup_result(executor: TestExecutor, outputs_dir: Path) -> list[WorkflowResult] | None:
    if should_skip_playwright_executor(executor):
        details = (
            "skipped Playwright executor on windows enterprise configuration; "
            "Playwright workflows are dispatched by the ubuntu enterprise configuration run"
        )
        log_progress(f"    {details}")
        return [synthetic_result(executor, outputs_dir, "_skipped", STATUS_SKIPPED, details, 0)]

    if executor.result_profile is not None:
        log_progress(f"    using synthetic result profile for {executor.type}/{executor.name}")
        return [profiled_result(executor, outputs_dir)]

    if executor.command:
        return [
            synthetic_result(
                executor,
                outputs_dir,
                "_configured",
                STATUS_SETUP_FAILED,
                "orchestrator dispatches GitHub workflow files; configured command-only entries are not supported",
                1,
            )
        ]

    repo_slug = github_repo_slug(executor.github_url)
    if not repo_slug:
        return [synthetic_result(executor, outputs_dir, "_setup", STATUS_MISSING_REPO_URL, "could not determine GitHub repository", 1)]

    return None


def discover_parallel_workflow_selection(
    executor: TestExecutor,
    outputs_dir: Path,
    repo_slug: str,
    ref: str,
    github_token: str,
    api_base_url: str,
) -> tuple[list[WorkflowResult], ParallelWorkflowSelection | None]:
    log_progress(f"    discovering workflows via GitHub API in {repo_slug} on {ref}")
    try:
        discovered_workflow_files = discover_remote_workflow_files(
            repo_slug=repo_slug,
            ref=ref,
            executor=executor,
            token=github_token,
            api_base_url=api_base_url,
        )
    except Exception as exc:
        return [
            synthetic_result(
                executor,
                outputs_dir,
                "_discovery",
                STATUS_SETUP_FAILED,
                f"could not discover GitHub workflows via API for {repo_slug}: {exc}",
                1,
            )
        ], None

    discovered_labels = [workflow_file.label for workflow_file in discovered_workflow_files]
    if discovered_labels:
        log_progress(
            "    remote workflow files returned by GitHub API: "
            + ", ".join(sorted(discovered_labels))
        )
    else:
        log_progress("    remote workflow files returned by GitHub API: none")

    candidate_workflow_files = [
        workflow_file
        for workflow_file in discovered_workflow_files
        if should_consider_workflow_for_execution_text(workflow_file.text, workflow_file.label)
    ]
    candidate_labels = [workflow_file.label for workflow_file in candidate_workflow_files]
    filtered_out_labels = [
        workflow_file.label for workflow_file in discovered_workflow_files if workflow_file.label not in set(candidate_labels)
    ]
    if candidate_labels:
        log_progress(
            "    workflow files considered runnable by orchestrator: "
            + ", ".join(sorted(candidate_labels))
        )
    else:
        log_progress("    workflow files considered runnable by orchestrator: none")
    if filtered_out_labels:
        log_progress(
            "    workflow files filtered out before workflow_dispatch check: "
            + ", ".join(sorted(filtered_out_labels))
        )

    workflow_files = [
        workflow_file for workflow_file in candidate_workflow_files if has_workflow_dispatch_trigger_in_text(workflow_file.text)
    ]
    non_dispatchable_workflow_files = [
        workflow_file for workflow_file in candidate_workflow_files if not has_workflow_dispatch_trigger_in_text(workflow_file.text)
    ]
    if workflow_files:
        log_progress(
            "    dispatchable workflow files: "
            + ", ".join(sorted(workflow_file.label for workflow_file in workflow_files))
        )
    else:
        log_progress("    dispatchable workflow files: none")
    if non_dispatchable_workflow_files:
        log_progress(
            "    runnable workflow files without workflow_dispatch: "
            + ", ".join(sorted(workflow_file.label for workflow_file in non_dispatchable_workflow_files))
        )
    log_progress(
        f"    discovered {len(workflow_files)} dispatchable workflow file"
        f"{'s' if len(workflow_files) != 1 else ''} in {repo_slug}"
    )
    if non_dispatchable_workflow_files:
        log_progress(
            "    skipped "
            f"{len(non_dispatchable_workflow_files)} workflow file"
            f"{'s' if len(non_dispatchable_workflow_files) != 1 else ''} without workflow_dispatch"
        )
    if not workflow_files and not non_dispatchable_workflow_files:
        return [synthetic_result(executor, outputs_dir, "_discovery", STATUS_NO_WORKFLOWS, "no workflow files found", 1)], None

    return [], ParallelWorkflowSelection(
        dispatchable=workflow_files,
        non_dispatchable=non_dispatchable_workflow_files,
    )


def non_dispatchable_workflow_results(
    executor: TestExecutor,
    outputs_dir: Path,
    workflow_files: list[RemoteWorkflowFile],
) -> list[WorkflowResult]:
    results: list[WorkflowResult] = []
    for workflow_file in workflow_files:
        workflow_label = workflow_file.label
        results.append(
            synthetic_result(
                executor,
                outputs_dir,
                Path(workflow_label).stem,
                STATUS_SETUP_FAILED,
                (
                    f"{workflow_label} cannot be dispatched because it does not declare workflow_dispatch. "
                    "Add an on.workflow_dispatch trigger to this target workflow, or remove it from the parallel manifest."
                ),
                1,
            )
        )
    return results


def dispatch_inputs_for_remote_workflow(
    executor: TestExecutor,
    workflow_file: RemoteWorkflowFile,
    specmatic_version: str,
    enterprise_version: str,
    enterprise_docker_image: str,
    jar_url: str,
    jar_path: str,
) -> dict[str, str]:
    available_inputs = extract_workflow_dispatch_inputs_from_text(workflow_file.text)
    additional_env_map = parse_additional_env_variables(executor.additional_env_variables)
    orchestrator_disable_visual = additional_env_map.get(
        "ORCHESTRATOR_DISABLE_VISUAL",
        os.environ.get("ORCHESTRATOR_DISABLE_VISUAL", "true"),
    )
    return workflow_dispatch_inputs_for(
        available_inputs=available_inputs,
        specmatic_version=specmatic_version,
        enterprise_version=enterprise_version,
        enterprise_docker_image=enterprise_docker_image,
        jar_url=jar_url,
        jar_path=jar_path,
        orchestrator_disable_visual=orchestrator_disable_visual,
    )


def dispatch_remote_workflow(
    executor: TestExecutor,
    outputs_dir: Path,
    repo_slug: str,
    ref: str,
    workflow_file: RemoteWorkflowFile,
    workflow_index: int,
    workflow_count: int,
    github_token: str,
    api_base_url: str,
    specmatic_version: str,
    enterprise_version: str,
    enterprise_docker_image: str,
    jar_url: str,
    jar_path: str,
) -> tuple[WorkflowResult | None, ParallelWorkflowRun | None]:
    workflow_label = workflow_file.label
    inputs = dispatch_inputs_for_remote_workflow(
        executor=executor,
        workflow_file=workflow_file,
        specmatic_version=specmatic_version,
        enterprise_version=enterprise_version,
        enterprise_docker_image=enterprise_docker_image,
        jar_url=jar_url,
        jar_path=jar_path,
    )
    started_at = utc_now()
    dispatched_after = datetime.now(timezone.utc)
    log_progress(
        f"  -> dispatching workflow {workflow_index}/{workflow_count}: {workflow_label} in {repo_slug} on {ref}"
    )
    if inputs:
        log_progress(
            "     dispatch inputs: "
            + ", ".join(f"{key}={value}" for key, value in sorted(inputs.items()))
        )
    else:
        log_progress("     dispatch inputs: none")

    try:
        dispatch_github_workflow(
            repo_slug=repo_slug,
            workflow_label=workflow_label,
            ref=ref,
            inputs=inputs,
            token=github_token,
            api_base_url=api_base_url,
        )
    except Exception as exc:
        return synthetic_result(
            executor,
            outputs_dir,
            Path(workflow_label).stem,
            STATUS_SETUP_FAILED,
            f"workflow_dispatch failed for {workflow_label}: {exc}",
            1,
        ), None

    orchestrator_run_suffix = inputs.get("orchestrator_run_suffix") or ""
    return None, ParallelWorkflowRun(
        workflow_label=workflow_label,
        started_at=started_at,
        dispatched_after=dispatched_after,
        ref=ref,
        dispatch_started_monotonic=time.time(),
        executor=executor,
        repo_slug=repo_slug,
        expected_run_title_fragment=orchestrator_run_suffix,
    )


def dispatch_remote_workflows(
    executor: TestExecutor,
    outputs_dir: Path,
    repo_slug: str,
    ref: str,
    workflow_files: list[RemoteWorkflowFile],
    github_token: str,
    api_base_url: str,
    specmatic_version: str,
    enterprise_version: str,
    enterprise_docker_image: str,
    jar_url: str,
    jar_path: str,
) -> tuple[list[WorkflowResult], list[ParallelWorkflowRun]]:
    dispatch_delay_seconds = 5
    if workflow_files:
        log_progress(f"    preparing workflow_dispatch requests for {len(workflow_files)} workflow(s)")

    dispatch_errors: list[WorkflowResult] = []
    dispatched: list[ParallelWorkflowRun] = []
    for index, workflow_file in enumerate(workflow_files, start=1):
        error_result, run = dispatch_remote_workflow(
            executor=executor,
            outputs_dir=outputs_dir,
            repo_slug=repo_slug,
            ref=ref,
            workflow_file=workflow_file,
            workflow_index=index,
            workflow_count=len(workflow_files),
            github_token=github_token,
            api_base_url=api_base_url,
            specmatic_version=specmatic_version,
            enterprise_version=enterprise_version,
            enterprise_docker_image=enterprise_docker_image,
            jar_url=jar_url,
            jar_path=jar_path,
        )
        if error_result is not None:
            dispatch_errors.append(error_result)
        if run is not None:
            dispatched.append(run)
            if index < len(workflow_files):
                next_index = index + 1
                log_progress(
                    "    waiting "
                    f"{dispatch_delay_seconds}s before dispatching workflow {next_index}/{len(workflow_files)} "
                    f"for {repo_slug}"
                )
                time.sleep(dispatch_delay_seconds)

    if dispatched:
        if len(dispatched) == len(workflow_files):
            log_progress(f"    Dispatched successfully: {len(dispatched)}/{len(workflow_files)} workflows")
        else:
            log_progress(
                f"    Dispatch completed: {len(dispatched)}/{len(workflow_files)} workflows dispatched successfully"
            )
    return dispatch_errors, dispatched


def update_parallel_workflow_run(
    item: ParallelWorkflowRun,
    now: float,
    github_token: str,
    api_base_url: str,
    timeout_seconds: int,
) -> bool:
    if item.completed_run is not None or item.error_message:
        return True

    elapsed_seconds = int(now - item.dispatch_started_monotonic)
    if elapsed_seconds >= timeout_seconds:
        item.error_message = (
            f"workflow_dispatch polling failed for {item.workflow_label}: "
            f"timed out after {timeout_seconds}s"
        )
        return True

    try:
        if item.run_id is None:
            run = find_dispatched_workflow_run_once(
                repo_slug=item.repo_slug,
                workflow_label=item.workflow_label,
                branch=item.ref,
                dispatched_after=item.dispatched_after,
                token=github_token,
                api_base_url=api_base_url,
                expected_run_title_fragment=item.expected_run_title_fragment,
            )
            if run is None:
                return False
            item.run_id = int(run["id"])
            item.html_url = str(run.get("html_url") or "")
            item.github_status = str(run.get("status") or "queued")
            item.conclusion = str(run.get("conclusion") or "")
            if item.github_status == "completed":
                item.completed_run = run
            return item.completed_run is not None

        run = github_api_json(
            "GET",
            f"{api_base_url}/repos/{item.repo_slug}/actions/runs/{item.run_id}",
            github_token,
        )
        item.html_url = str(run.get("html_url") or item.html_url)
        item.github_status = str(run.get("status") or "unknown")
        item.conclusion = str(run.get("conclusion") or "")
        if item.github_status == "completed":
            item.completed_run = run
    except Exception as exc:
        item.error_message = f"workflow_dispatch polling failed for {item.workflow_label}: {exc}"
        return True

    return item.completed_run is not None


def should_log_parallel_progress(
    logged_initial_progress: bool,
    now: float,
    last_progress_log_time: float,
    all_finished: bool,
) -> bool:
    return (
        not logged_initial_progress
        or now - last_progress_log_time >= PARALLEL_PROGRESS_LOG_INTERVAL_SECONDS
        or all_finished
    )


def parallel_workflow_result(
    item: ParallelWorkflowRun,
    outputs_dir: Path,
    github_token: str,
    api_base_url: str,
) -> WorkflowResult | None:
    if item.executor is None:
        return None
    if item.completed_run is not None:
        result = workflow_result_from_github_run(
            executor=item.executor,
            outputs_dir=outputs_dir,
            repo_slug=item.repo_slug,
            workflow_label=item.workflow_label,
            run=item.completed_run,
            started_at=item.started_at,
            elapsed_seconds=int(time.time() - item.dispatch_started_monotonic),
            github_token=github_token,
            api_base_url=api_base_url,
        )
        log_progress(
            f"     completed {item.repo_slug}/{Path(item.workflow_label).stem}: "
            f"status={result.status}, time={format_elapsed_time(result.duration_seconds)}, "
            f"output={result.output_dir}"
        )
        return result

    return synthetic_result(
        item.executor,
        outputs_dir,
        Path(item.workflow_label).stem,
        STATUS_SETUP_FAILED,
        item.error_message or f"workflow_dispatch polling failed for {item.workflow_label}",
        1,
    )


def dispatch_parallel_executor_workflows(
    executor: TestExecutor,
    outputs_dir: Path,
    github_token: str,
    api_base_url: str,
    specmatic_version: str = "",
    enterprise_version: str = "",
    enterprise_docker_image: str = "",
    jar_url: str = "",
    jar_path: str = "",
) -> tuple[list[WorkflowResult], list[ParallelWorkflowRun]]:
    setup_results = parallel_executor_setup_result(executor, outputs_dir)
    if setup_results is not None:
        return setup_results, []

    repo_slug = github_repo_slug(executor.github_url)
    assert repo_slug is not None
    ref = executor.branch or "main"
    discovery_results, workflow_selection = discover_parallel_workflow_selection(
        executor=executor,
        outputs_dir=outputs_dir,
        repo_slug=repo_slug,
        ref=ref,
        github_token=github_token,
        api_base_url=api_base_url,
    )
    if workflow_selection is None:
        return discovery_results, []

    non_dispatchable_results = non_dispatchable_workflow_results(
        executor,
        outputs_dir,
        workflow_selection.non_dispatchable,
    )
    dispatch_errors, dispatched = dispatch_remote_workflows(
        executor=executor,
        outputs_dir=outputs_dir,
        repo_slug=repo_slug,
        ref=ref,
        workflow_files=workflow_selection.dispatchable,
        github_token=github_token,
        api_base_url=api_base_url,
        specmatic_version=specmatic_version,
        enterprise_version=enterprise_version,
        enterprise_docker_image=enterprise_docker_image,
        jar_url=jar_url,
        jar_path=jar_path,
    )
    return discovery_results + non_dispatchable_results + dispatch_errors, dispatched


def wait_for_parallel_workflows(
    dispatched: list[ParallelWorkflowRun],
    outputs_dir: Path,
    github_token: str,
    api_base_url: str,
    poll_seconds: int,
    timeout_seconds: int,
) -> list[WorkflowResult]:
    if not dispatched:
        return []

    log_progress(f"==> Waiting for {len(dispatched)} dispatched workflow(s) across all repositories")
    last_progress_log_time = 0.0
    logged_initial_progress = False
    rendered_progress_snapshot_count = 0
    while True:
        now = time.time()
        workflow_states = [
            update_parallel_workflow_run(
                item,
                now,
                github_token,
                api_base_url,
                timeout_seconds,
            )
            for item in dispatched
        ]
        all_finished = all(workflow_states)

        if should_log_parallel_progress(logged_initial_progress, now, last_progress_log_time, all_finished):
            rendered_progress_snapshot_count += 1
            log_progress(render_parallel_progress_table(dispatched, rendered_progress_snapshot_count))
            last_progress_log_time = now
            logged_initial_progress = True

        if all_finished:
            break
        time.sleep(max(1, poll_seconds))

    return [
        result
        for item in dispatched
        if (result := parallel_workflow_result(item, outputs_dir, github_token, api_base_url)) is not None
    ]


def build_summary(results: list[WorkflowResult]) -> dict[str, Any]:
    successful_statuses = {STATUS_PASSED, STATUS_SKIPPED, STATUS_NEUTRAL}
    failed = [result for result in results if result.status not in successful_statuses]
    repos_where_tests_ran = sorted({result.repository for result in results if result.executed_commands})
    repos_where_tests_did_not_run = sorted({result.repository for result in results if not result.executed_commands})
    error_summary = build_error_summary(failed)
    non_dispatchable_workflows = [result for result in results if is_non_dispatchable_workflow_result(result)]

    return {
        "conclusion": "success" if not failed else "failure",
        "total": len(results),
        "passed_count": len(results) - len(failed),
        "failed_count": len(failed),
        "numberOfReposIncluded": len({result.repository for result in results}),
        "reposWhereTestsRan": repos_where_tests_ran,
        "reposWhereTestsDidNotRun": repos_where_tests_did_not_run,
        "total_tests": sum(result.total_tests for result in results),
        "failed_tests": sum(result.failed_tests for result in results),
        "skipped_tests": sum(result.skipped_tests for result in results),
        "error_summary": error_summary,
        "non_dispatchable_workflows": [asdict(result) for result in non_dispatchable_workflows],
        "results": [asdict(result) for result in results],
    }


def is_non_dispatchable_workflow_result(result: WorkflowResult) -> bool:
    return (
        result.status == STATUS_SETUP_FAILED
        and "does not declare workflow_dispatch" in result.details.lower()
    )


def dispatchable_results(results: list[WorkflowResult]) -> list[WorkflowResult]:
    return [result for result in results if not is_non_dispatchable_workflow_result(result)]


def concise_result_details(details: str) -> str:
    value = str(details or "").strip()
    if not value:
        return "n/a"
    for token in value.replace("(", " ").replace(")", " ").split():
        if token.startswith("http://") or token.startswith("https://"):
            return token.rstrip(".,;")
    if len(value) > 120:
        return value[:117] + "..."
    return value


def actionable_step_for_result(result: WorkflowResult) -> str:
    details = result.details.lower()
    if "does not declare workflow_dispatch" in details:
        return (
            "Add workflow_dispatch to the target workflow, or list only dispatchable workflows in the manifest."
        )
    if "workflow_dispatch failed" in details and ("404" in details or "not found" in details):
        return (
            "Confirm the workflow file exists on the target branch and declares workflow_dispatch. "
            "Also confirm the token can access the target repository."
        )
    if "workflow_dispatch failed" in details and ("422" in details or "workflow does not have" in details):
        return (
            "Check the target workflow's workflow_dispatch inputs and required values. "
            "The orchestrator only sends inputs declared by that workflow."
        )
    if "timed out waiting for dispatched run" in details:
        return (
            "Confirm the dispatch created a run in the target repo Actions tab, the target branch is correct, "
            "and the token has actions read access."
        )
    if result.status == STATUS_SETUP_FAILED:
        return "Open the linked run.log for the setup error, then fix the target workflow/repository setup before re-running."
    if result.status == STATUS_COMMAND_FAILED:
        return "Open run.log for the failing command and fix the command, dependency, or environment reported there."
    if result.failed_tests:
        return "Open the copied test reports or run.log, fix the failing tests, then re-run the orchestrator."
    return "Open run.log for details and re-run after fixing the reported failure."


def build_error_summary(failed_results: list[WorkflowResult]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for result in failed_results:
        entries.append(
            {
                "repository": f"{result.type}/{result.repository}",
                "workflow": result.workflow,
                "status": result.status,
                "error": result.details,
                "action": actionable_step_for_result(result),
                "log": result.log_file,
            }
        )
    return entries


def render_error_summary(error_summary: list[dict[str, str]], limit: int = 20) -> str:
    if not error_summary:
        return ""
    lines = ["Error Summary and Actionable Steps"]
    for index, entry in enumerate(error_summary[:limit], start=1):
        lines.extend(
            [
                f"{index}. {entry['repository']} / {Path(entry['workflow']).stem} [{entry['status']}]",
                f"   Error: {entry['error']}",
                f"   Action: {entry['action']}",
                f"   Log: {entry['log']}",
            ]
        )
    remaining = len(error_summary) - limit
    if remaining > 0:
        lines.append(f"... {remaining} more failure(s). See outputs/orchestration-summary.json for the full list.")
    return "\n".join(lines)


def render_summary_table(results: list[WorkflowResult]) -> str:
    headers = ["Repository", "Workflow", "Status", "Tests", "Failed", "Skipped", "Commands", "Completed in", "Log"]
    rows = [
        [
            f"{result.type}/{result.repository}",
            Path(result.workflow).stem,
            status_symbol(result.status),
            str(result.total_tests),
            str(result.failed_tests),
            str(result.skipped_tests),
            str(len(result.executed_commands)),
            format_elapsed_time(result.duration_seconds),
            result.log_file,
        ]
        for result in results
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows)) if rows else len(headers[index])
        for index in range(len(headers))
    ]

    def render_row(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join([render_row(headers), separator, *(render_row(row) for row in rows)])


def render_non_dispatchable_workflow_table(results: list[WorkflowResult]) -> str:
    skipped_results = [result for result in results if is_non_dispatchable_workflow_result(result)]
    if not skipped_results:
        return ""
    headers = ["Repository", "Workflow", "Reason", "Log"]
    rows = [
        [
            f"{result.type}/{result.repository}",
            Path(result.workflow).stem,
            "missing workflow_dispatch",
            result.log_file,
        ]
        for result in skipped_results
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render_row(values: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    separator = "-+-".join("-" * width for width in widths)
    return "\n".join([render_row(headers), separator, *(render_row(row) for row in rows)])


def status_symbol(status: str) -> str:
    if status == STATUS_SKIPPED:
        return "⏭️"
    if status == STATUS_CANCELLED:
        return "🚫"
    if status == STATUS_TIMED_OUT:
        return "⏱️"
    if status == STATUS_ACTION_REQUIRED:
        return "⚠️"
    if status == STATUS_NEUTRAL:
        return "➖"
    if status == STATUS_STARTUP_FAILURE:
        return "⚠️"
    return "✅" if status in {STATUS_PASSED, "PASSED"} else "❌"


def collect_report_file_entries(output_dir: Path) -> tuple[list[Path], list[Path], list[Path]]:
    ignored = {"result.json", "run.log"}
    all_files = [path for path in sorted(output_dir.rglob("*")) if path.is_file() and path.name not in ignored]
    specmatic_html_files: list[Path] = []
    ctrf_files: list[Path] = []
    playwright_html_files: list[Path] = []
    for path in all_files:
        parts = [part.lower() for part in path.parts]
        if "specmatic" in parts:
            if "html" in parts:
                specmatic_html_files.append(path)
            if "ctrf" in parts:
                ctrf_files.append(path)

        # Playwright outputs use playwright-report*/index.html (no CTRF tree).
        if path.name.lower() == "index.html":
            if any(part.startswith("playwright-report") or part.startswith("playwright_report") for part in parts):
                playwright_html_files.append(path)

    html_files = sorted(dict.fromkeys(specmatic_html_files + playwright_html_files))
    report_files = sorted(dict.fromkeys(html_files + ctrf_files))
    return html_files, ctrf_files, report_files


def report_path_priority(path: Path) -> int:
    path_text = path.as_posix().lower()
    if "/build/reports/specmatic/" in path_text:
        return 3
    if "/reports/specmatic/" in path_text:
        return 2
    return 1


def report_scope(path: Path, marker: str) -> str:
    lower_parts = [part.lower() for part in path.parts]
    try:
        marker_index = lower_parts.index(marker)
    except ValueError:
        return "root"
    specmatic_indexes = [
        index for index, part in enumerate(lower_parts[:marker_index])
        if part == "specmatic"
    ]
    if not specmatic_indexes:
        return "root"
    specmatic_index = specmatic_indexes[-1]
    if marker_index <= specmatic_index:
        return "root"
    scope_parts = path.parts[specmatic_index + 1 : marker_index]
    if not scope_parts:
        return "root"
    return "/".join(scope_parts)


def pick_preferred_by_scope(paths: list[Path], marker: str, prefer_ctrf_name: str = "") -> list[tuple[str, Path]]:
    selected: dict[str, Path] = {}
    for path in paths:
        scope = report_scope(path, marker)
        existing = selected.get(scope)
        if existing is None:
            selected[scope] = path
            continue

        existing_score = report_path_priority(existing)
        candidate_score = report_path_priority(path)
        if prefer_ctrf_name:
            if existing.name.lower() == prefer_ctrf_name:
                existing_score += 1
            if path.name.lower() == prefer_ctrf_name:
                candidate_score += 1

        if candidate_score > existing_score:
            selected[scope] = path

    return sorted(selected.items(), key=lambda item: item[0])


def to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_ctrf_summary(ctrf_file: Path) -> tuple[int, int, int, int]:
    try:
        payload = json.loads(ctrf_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0, 0, 0

    summary = payload.get("results", {}).get("summary", {})
    tests = to_int(summary.get("tests"))
    passed = to_int(summary.get("passed"))
    failed = to_int(summary.get("failed"))
    skipped = to_int(summary.get("skipped"))
    return tests, passed, failed, skipped


def render_file_entries(workflow_page: Path, files: list[Path], empty_message: str, limit: int = 25) -> str:
    if not files:
        return f"<li>{html_escape(empty_message)}</li>"

    entries: list[str] = []
    for entry in files[:limit]:
        entries.append(
            f'<li><a href="{html_escape(relative_href(workflow_page, entry))}">{html_escape(entry.relative_to(workflow_page.parent))}</a></li>'
        )
    remaining = len(files) - limit
    if remaining > 0:
        entries.append(f"<li>+ {remaining} more file(s)</li>")
    return "".join(entries)


def render_workflow_page(result: WorkflowResult, outputs_dir: Path) -> None:
    workflow_dir = Path(result.output_dir)
    workflow_page = workflow_dir / "index.html"
    dashboard_path = outputs_dir / "index.html"
    html_files, ctrf_files, _ = collect_report_file_entries(workflow_dir)

    primary_html_indexes = [path for path in html_files if path.name.lower() == "index.html"]
    html_primary = pick_preferred_by_scope(primary_html_indexes, marker="html")
    ctrf_primary = pick_preferred_by_scope(ctrf_files, marker="ctrf", prefer_ctrf_name="ctrf-report.json")

    html_primary_entries = []
    for scope, entry in html_primary:
        html_dir = entry.parent
        file_count = sum(1 for child in html_dir.rglob("*") if child.is_file())
        html_primary_entries.append(
            "<li>"
            f"<strong>{html_escape(scope)}</strong>: "
            f'<a href="{html_escape(relative_href(workflow_page, entry))}">{html_escape(entry.relative_to(workflow_dir))}</a> '
            f"({file_count} file(s))"
            "</li>"
        )

    ctrf_summary_rows = []
    for scope, entry in ctrf_primary:
        tests, passed, failed, skipped = parse_ctrf_summary(entry)
        ctrf_summary_rows.append(
            "<tr>"
            f"<td>{html_escape(scope)}</td>"
            f'<td><a href="{html_escape(relative_href(workflow_page, entry))}">{html_escape(entry.relative_to(workflow_dir))}</a></td>'
            f"<td>{html_escape(tests)}</td>"
            f"<td>{html_escape(passed)}</td>"
            f"<td>{html_escape(failed)}</td>"
            f"<td>{html_escape(skipped)}</td>"
            "</tr>"
        )

    command_entries = [
        "<li>"
        f"<strong>{html_escape(command.step_name)}</strong> "
        f"<code>{html_escape(command.command)}</code> "
        f"(dir: <code>{html_escape(command.working_directory)}</code>, exit: {html_escape(command.exit_code)}, time: {html_escape(format_elapsed_time(command.duration_seconds))})"
        "</li>"
        for command in result.executed_commands
    ]

    content = read_template("project_report.html").substitute(
        page_title=html_escape(f"{result.type}/{result.repository} - {Path(result.workflow).stem}"),
        style_css=read_template_text("project_report.css"),
        dashboard_href=html_escape(relative_href(workflow_page, dashboard_path)),
        workflow_name=html_escape(result.workflow),
        repo_name=html_escape(f"{result.type}/{result.repository}"),
        workflow_status_class=html_escape(result.status),
        workflow_status_label=status_symbol(result.status),
        repo_ref=html_escape(f"{result.branch or 'default'}"),
        workflow_duration=html_escape(format_elapsed_time(result.duration_seconds)),
        workflow_total_tests=html_escape(result.total_tests),
        workflow_failures=html_escape(result.failed_tests),
        workflow_skipped=html_escape(result.skipped_tests),
        repo_url=html_escape(result.repo_url),
        workflow_exit_code=html_escape(result.exit_code),
        workflow_details=html_escape(result.details),
        workflow_log_href=html_escape(relative_href(workflow_page, Path(result.log_file))),
        command_entries="".join(command_entries) if command_entries else "<li>No commands executed.</li>",
        html_primary_entries="".join(html_primary_entries) if html_primary_entries else "<li>No HTML index reports copied.</li>",
        html_file_count=html_escape(len(html_files)),
        ctrf_file_count=html_escape(len(ctrf_files)),
        html_entries=render_file_entries(workflow_page, html_files, "No HTML report files copied."),
        ctrf_entries=render_file_entries(workflow_page, ctrf_files, "No CTRF report files copied."),
        ctrf_summary_rows="".join(ctrf_summary_rows)
        if ctrf_summary_rows
        else "<tr><td colspan='6'>No CTRF report files copied.</td></tr>",
    )
    write_text(workflow_page, content)


def render_dashboard(outputs_dir: Path, summary: dict[str, Any], results: list[WorkflowResult]) -> None:
    status_class = "passed" if summary["conclusion"] == "success" else "failed"
    sorted_results = sorted(dispatchable_results(results), key=lambda item: (item.status == STATUS_PASSED, -item.failed_tests, -item.duration_seconds))

    rows = []
    for result in sorted_results:
        page_path = Path(result.output_dir) / "index.html"
        badge_class = "passed" if result.status in {STATUS_PASSED, STATUS_SKIPPED, STATUS_NEUTRAL} else "failed"
        rows.append(
            f"""
            <tr>
              <td><a href="{html_escape(relative_href(outputs_dir / "index.html", page_path))}">{html_escape(result.type + "/" + result.repository)}</a></td>
              <td>{html_escape(Path(result.workflow).stem)}</td>
              <td><span class="badge {badge_class}">{status_symbol(result.status)}</span></td>
              <td>{html_escape(format_elapsed_time(result.duration_seconds))}</td>
              <td>{html_escape(result.failed_tests)}</td>
              <td>{html_escape(result.total_tests)}</td>
              <td>{html_escape(result.skipped_tests)}</td>
              <td>{html_escape(concise_result_details(result.details))}</td>
            </tr>
            """
        )
    non_dispatchable_rows = []
    for result in sorted((result for result in results if is_non_dispatchable_workflow_result(result)), key=lambda item: (item.type, item.repository, item.workflow)):
        page_path = Path(result.output_dir) / "index.html"
        non_dispatchable_rows.append(
            f"""
            <tr>
              <td><a href="{html_escape(relative_href(outputs_dir / "index.html", page_path))}">{html_escape(result.type + "/" + result.repository)}</a></td>
              <td>{html_escape(Path(result.workflow).stem)}</td>
              <td>missing workflow_dispatch</td>
              <td>{html_escape(result.log_file)}</td>
            </tr>
            """
        )
    non_dispatchable_section = ""
    if non_dispatchable_rows:
        non_dispatchable_section = f"""
    <div class="panel">
      <h2>Skipped Workflows Without workflow_dispatch</h2>
      <table>
        <thead>
          <tr>
            <th>Repository</th>
            <th>Workflow</th>
            <th>Reason</th>
            <th>Log</th>
          </tr>
        </thead>
        <tbody>
{''.join(non_dispatchable_rows)}
        </tbody>
      </table>
    </div>
        """

    content = read_template("dashboard.html").substitute(
        style_css=read_template_text("dashboard.css"),
        status_class=status_class,
        overall_status=html_escape(summary["conclusion"].upper()),
        configured=html_escape(summary["numberOfReposIncluded"]),
        passed=html_escape(summary["passed_count"]),
        failed=html_escape(summary["failed_count"]),
        total_tests=html_escape(summary["total_tests"]),
        failed_tests=html_escape(summary["failed_tests"]),
        skipped_tests=html_escape(summary["skipped_tests"]),
        generated_at_utc=html_escape(utc_now()),
        project_rows="".join(rows),
        non_dispatchable_section=non_dispatchable_section,
    )
    write_text(outputs_dir / "index.html", content)


def render_html_reports(outputs_dir: Path, summary: dict[str, Any], results: list[WorkflowResult]) -> None:
    for result in results:
        render_workflow_page(result, outputs_dir)
    render_dashboard(outputs_dir, summary, results)


def main() -> int:
    args = parse_args()
    enterprise_version_error = validate_required_enterprise_version(args)
    if enterprise_version_error:
        print(enterprise_version_error, file=sys.stderr)
        return 1

    config_path = resolve_config_path(args.config)
    log_progress(
        "Test executor manifest: "
        f"requested config={args.config or 'not provided'}, "
        f"resolved path={config_path}"
    )
    if not args.config:
        print(
            "No test executor manifest was provided. "
            "Pass caller-owned test executor JSON through orchestrator_options.test_executor_json "
            "or pass --config for local testing.",
            file=sys.stderr,
        )
        return 1
    if not config_path.exists():
        print(f"Config file not found: {config_path}", file=sys.stderr)
        return 1

    requested_enterprise_version = args.enterprise_version
    try:
        enterprise_artifact = resolve_enterprise_artifact_inputs(
            args.enterprise_version,
            args.specmatic_jar_url,
            args.specmatic_jar_path,
        )
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"Could not resolve ENTERPRISE_VERSION {args.enterprise_version!r}: {exc}", file=sys.stderr)
        return 1
    args.enterprise_version = enterprise_artifact.version
    if enterprise_artifact.jar_url:
        args.specmatic_jar_url = enterprise_artifact.jar_url
    os.environ["ENTERPRISE_VERSION"] = enterprise_artifact.version
    if args.specmatic_jar_url:
        os.environ["SPECMATIC_JAR_URL"] = args.specmatic_jar_url
    if args.specmatic_jar_path:
        os.environ["SPECMATIC_JAR_PATH"] = args.specmatic_jar_path
    log_progress(
        "Enterprise artifact resolution: "
        f"requested ENTERPRISE_VERSION={requested_enterprise_version!r}, "
        f"resolved enterprise_version={enterprise_artifact.version!r}, "
        f"resolved jar_url={args.specmatic_jar_url or 'n/a'}, "
        f"resolved jar_path={args.specmatic_jar_path or 'n/a'}"
    )

    executors = load_executors(config_path)
    if not executors:
        print(f"No test executors configured in {config_path}", file=sys.stderr)
        return 1

    temp_dir = Path(args.temp_dir)
    outputs_dir = Path(args.outputs_dir)
    try:
        clean_temp_dir(temp_dir)
        clean_outputs_dir(outputs_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    all_results: list[WorkflowResult] = []
    applied_overrides: dict[str, dict[str, str]] = {}
    parallel_github_token = (
        os.environ.get("ORCHESTRATOR_GITHUB_TOKEN")
        or os.environ.get("SPECMATIC_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    )
    github_api_base_url = os.environ.get("GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")
    if not parallel_github_token:
        print(
            "Specmatic orchestration requires ORCHESTRATOR_GITHUB_TOKEN, SPECMATIC_GITHUB_TOKEN, or GITHUB_TOKEN "
            "with permission to dispatch and read target repository workflow runs.",
            file=sys.stderr,
        )
        return 1

    parallel_dispatched: list[ParallelWorkflowRun] = []

    for executor in executors:
        effective_specmatic_version = args.specmatic_version or executor.specmatic_version or os.environ.get("SPECMATIC_VERSION", "")
        effective_enterprise_version = args.enterprise_version or executor.enterprise_version or os.environ.get("ENTERPRISE_VERSION", "")
        effective_enterprise_docker_image = (
            args.enterprise_docker_image
            or executor.enterprise_docker_image
            or os.environ.get("ENTERPRISE_DOCKER_IMAGE", "")
            or os.environ.get("SPECMATIC_STUDIO_DOCKER_IMAGE", "")
        )
        applied_overrides[f"{executor.type}/{executor.name}"] = {
            "specmatic_version": effective_specmatic_version,
            "enterprise_version": effective_enterprise_version,
            "enterprise_docker_image": effective_enterprise_docker_image,
        }
        log_progress(f"==> Running {executor.type}/{executor.name}")
        if effective_specmatic_version or effective_enterprise_version or effective_enterprise_docker_image:
            log_progress(
                "    resolved overrides: "
                f"specmatic={effective_specmatic_version or 'n/a'}, "
                f"enterprise={effective_enterprise_version or 'n/a'}, "
                f"enterprise_docker_image={effective_enterprise_docker_image or 'n/a'}"
            )
        log_progress("    discovering and dispatching GitHub workflows")
        dispatch_results, dispatched = dispatch_parallel_executor_workflows(
            executor=executor,
            outputs_dir=outputs_dir,
            github_token=parallel_github_token,
            api_base_url=github_api_base_url,
            specmatic_version=effective_specmatic_version,
            enterprise_version=effective_enterprise_version,
            enterprise_docker_image=effective_enterprise_docker_image,
            jar_url=args.specmatic_jar_url,
            jar_path=args.specmatic_jar_path,
        )
        all_results.extend(dispatch_results)
        parallel_dispatched.extend(dispatched)

    all_results.extend(
        wait_for_parallel_workflows(
            dispatched=parallel_dispatched,
            outputs_dir=outputs_dir,
            github_token=parallel_github_token,
            api_base_url=github_api_base_url,
            poll_seconds=args.parallel_poll_seconds,
            timeout_seconds=args.parallel_timeout_seconds,
        )
    )

    summary = build_summary(all_results)
    summary["specmatic_version"] = args.specmatic_version
    summary["enterprise_version"] = args.enterprise_version
    summary["enterprise_docker_image"] = args.enterprise_docker_image
    summary["specmatic_jar_url"] = args.specmatic_jar_url
    summary["specmatic_jar_path"] = args.specmatic_jar_path
    summary["executor_overrides"] = applied_overrides
    write_json(outputs_dir / "orchestration-summary.json", summary)
    render_html_reports(outputs_dir, summary, all_results)

    log_progress("")
    log_progress("Test Orchestration Summary")
    log_progress(render_summary_table(dispatchable_results(all_results)))
    non_dispatchable_table = render_non_dispatchable_workflow_table(all_results)
    if non_dispatchable_table:
        log_progress("")
        log_progress("Skipped Workflows Without workflow_dispatch")
        log_progress(non_dispatchable_table)
    log_progress("")
    log_progress(
        "Overall: "
        f"{summary['conclusion']} | "
        f"workflows {summary['passed_count']}/{summary['total']} passed | "
        f"tests {summary['total_tests']} total, {summary['failed_tests']} failed, {summary['skipped_tests']} skipped"
    )
    rendered_error_summary = render_error_summary(summary.get("error_summary", []))
    if rendered_error_summary:
        log_progress("")
        log_progress(rendered_error_summary)
    if args.specmatic_version or args.enterprise_version or args.enterprise_docker_image:
        log_progress(
            "Version overrides: "
            f"specmatic={args.specmatic_version or 'n/a'}, "
            f"enterprise={args.enterprise_version or 'n/a'}, "
            f"enterprise_docker_image={args.enterprise_docker_image or 'n/a'}"
        )
    if args.specmatic_jar_url or args.specmatic_jar_path:
        log_progress(
            "Artifact overrides: "
            f"jar_url={args.specmatic_jar_url or 'n/a'}, "
            f"jar_path={args.specmatic_jar_path or 'n/a'}"
        )
    log_progress(f"JSON summary: {outputs_dir / 'orchestration-summary.json'}")
    log_progress(f"HTML dashboard: {outputs_dir / 'index.html'}")
    return 0 if summary["conclusion"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
