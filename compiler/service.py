"""Quarantine-gated dynamic skill compilation behind the Synth CLI head."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import json
import os
import re
import shutil
import tomllib
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

from compiler.gate import GatePolicy, GateResult, content_hash, evaluate
from middleware.telemetry import TelemetryEvent, TelemetryValidationError
from synth.config import SynthSettings, load_synth_settings
from synth.model import ModelCallError, local_model_callable
from synth.session import SessionTrajectory, load_session_trajectory

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REQUEST_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
MAX_WORKFLOW_CHARACTERS = 8_000
MAX_TRAJECTORY_CHARACTERS = 40_000
MAX_GENERATION_CHARACTERS = 256_000
MIN_REPEATED_SEQUENCE_LENGTH = 2
MAX_REPEATED_SEQUENCE_LENGTH = 5
AUDIT_SCHEMA_VERSION = 1
GATE_UV_CACHE = ".synth/skill-gate-uv-cache"
AUDIT_PATH = ".synth/audit/skills.jsonl"
LOCK_PATH = ".synth/locks/skills.lock"
DEMOTED_PATH = ".synth/demoted"
RUNTIME_CACHE_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache"})
RUNTIME_CACHE_SUFFIXES = frozenset({".pyc", ".pyo"})

GENERATION_SYSTEM_PROMPT = """You compile a recurring software-engineering workflow
into one safe, local, Python-backed Prime Agent skill package.

Compare the repeated actions with the surrounding trajectory. Keep only the critical
reusable sequence, generalize repository-specific paths and literal values into typed
arguments, and omit incidental one-off work.

Return exactly one JSON object with exactly these fields:
{
  "name": "lowercase-kebab-name",
  "reflection": "why this workflow is reusable and how the package captures it",
  "files": {
    "SKILL.md": "...",
    "pyproject.toml": "...",
    "src/<import_name>/__init__.py": "...",
    "tests/test_<import_name>.py": "..."
  }
}

Requirements:
- The name is at most 64 characters, uses lowercase letters, digits, and single
  hyphens, and <import_name> replaces hyphens with underscores.
- SKILL.md frontmatter has exactly the matching name and a concise description
  stating both when and when-NOT to use the skill.
- pyproject.toml uses hatchling, requires-python = ">=3.11", has dependencies = [],
  and packages exactly src/<import_name>.
- __init__.py exports exactly one top-level typed run() with a docstring. Use only
  the Python standard library and Python 3.11 syntax.
- The named test file imports run() and contains at least two meaningful behavioral
  test functions. Every case calls run() and asserts an observable result.
- The package must not access the network, spawn processes, execute dynamic code,
  invoke refinement or skill compilation, or write Synth control-plane files.
- Do not emit markdown fences, commentary, placeholders, extra files, or
  repository-specific secrets."""


class SkillCompilerError(RuntimeError):
    """Raised when a compiler operation cannot preserve its safety contract."""


@dataclass(frozen=True, slots=True)
class CompileRequest:
    session_path: Path
    request_id: str
    actor: str
    reason: str
    workflow: str | None = None
    name_hint: str | None = None
    fixture_path: Path | None = None


@dataclass(frozen=True, slots=True)
class GeneratedPackage:
    name: str
    reflection: str
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CompileOutcome:
    request_id: str
    session_id: str
    name: str | None
    status: str
    content_hash: str | None
    destination: Path | None
    gate: GateResult | None
    workflow: str

    def as_dict(self) -> dict[str, object]:
        return {
            "requestId": self.request_id,
            "sessionId": self.session_id,
            "name": self.name,
            "status": self.status,
            "contentHash": self.content_hash,
            "destination": (
                self.destination.as_posix() if self.destination is not None else None
            ),
            "gate": self.gate.to_dict() if self.gate is not None else None,
            "workflow": self.workflow,
        }


@dataclass(frozen=True, slots=True)
class DemotionOutcome:
    name: str
    content_hash: str
    destination: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "contentHash": self.content_hash,
            "destination": self.destination.as_posix(),
            "status": "demoted",
        }


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _filesystem_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def _safe_session_id(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)


def _workflow_digest(workflow: str) -> str:
    return hashlib.sha256(workflow.encode()).hexdigest()


def _validate_name(name: str) -> str:
    normalized = name.strip()
    if len(normalized) > 64 or SKILL_NAME_RE.fullmatch(normalized) is None:
        raise SkillCompilerError(
            "skill name must be at most 64 lowercase letters, digits, and "
            "single hyphens"
        )
    return normalized


def _validate_request_id(request_id: str) -> str:
    if REQUEST_ID_RE.fullmatch(request_id) is None:
        raise SkillCompilerError("compile request id must be a canonical UUIDv4")
    return request_id.lower()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SkillCompilerError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise SkillCompilerError(
            f"{label} must contain exactly: {', '.join(sorted(expected))}"
        )


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise SkillCompilerError(f"{label} must be a nonempty text string")
    return value


def _meaningful_test_error(source: str, import_name: str) -> str | None:
    try:
        tree = ast.parse(source, filename=f"test_{import_name}.py")
    except SyntaxError as error:
        return f"generated tests are invalid Python: {error.msg}"
    imported_run = any(
        isinstance(node, ast.ImportFrom)
        and node.module == import_name
        and any(alias.name == "run" for alias in node.names)
        for node in tree.body
    )
    if not imported_run:
        return f"generated tests must import run from {import_name}"
    cases = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    if len(cases) < 2:
        return "generated tests must define at least two test cases"
    for case in cases:
        calls_run = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
            for node in ast.walk(case)
        )
        asserts_result = any(isinstance(node, ast.Assert) for node in ast.walk(case))
        if not calls_run or not asserts_result:
            return f"{case.name} must call run() and assert its behavior"
    return None


def _validate_pyproject(source: str, name: str, import_name: str) -> None:
    try:
        value = tomllib.loads(source)
    except tomllib.TOMLDecodeError as error:
        raise SkillCompilerError(
            f"generated pyproject.toml is invalid: {error}"
        ) from error
    project = _mapping(value.get("project"), "pyproject.project")
    build = _mapping(value.get("build-system"), "pyproject.build-system")
    tool = _mapping(value.get("tool"), "pyproject.tool")
    hatch = _mapping(tool.get("hatch"), "pyproject.tool.hatch")
    hatch_build = _mapping(hatch.get("build"), "pyproject.tool.hatch.build")
    targets = _mapping(hatch_build.get("targets"), "pyproject.tool.hatch.build.targets")
    wheel = _mapping(targets.get("wheel"), "pyproject.tool.hatch.build.targets.wheel")
    if project.get("name") != name:
        raise SkillCompilerError("generated pyproject project name does not match")
    if project.get("requires-python") != ">=3.11":
        raise SkillCompilerError(
            'generated skills must declare requires-python = ">=3.11"'
        )
    if project.get("dependencies") != []:
        raise SkillCompilerError(
            "generated skills must initially use dependencies = []"
        )
    if build.get("build-backend") != "hatchling.build":
        raise SkillCompilerError("generated skills must use hatchling.build")
    requirements = build.get("requires")
    if (
        not isinstance(requirements, list)
        or not all(isinstance(item, str) for item in requirements)
        or not any(
            item.split("=", maxsplit=1)[0] == "hatchling" for item in requirements
        )
    ):
        raise SkillCompilerError("generated skills must declare hatchling")
    if wheel.get("packages") != [f"src/{import_name}"]:
        raise SkillCompilerError("generated wheel package mapping is invalid")


def parse_generated_package(response: str) -> GeneratedPackage:
    if len(response) > MAX_GENERATION_CHARACTERS:
        raise SkillCompilerError("generated package response exceeds the size cap")
    try:
        raw: object = json.loads(response)
    except json.JSONDecodeError as error:
        raise SkillCompilerError(
            "generated package response is not strict JSON"
        ) from error
    root = _mapping(raw, "generated package")
    _exact_keys(root, {"name", "reflection", "files"}, "generated package")
    name = _validate_name(_nonempty_string(root["name"], "generated package name"))
    reflection = _nonempty_string(root["reflection"], "generated reflection").strip()
    import_name = name.replace("-", "_")
    expected_paths = {
        "SKILL.md",
        "pyproject.toml",
        f"src/{import_name}/__init__.py",
        f"tests/test_{import_name}.py",
    }
    files_raw = _mapping(root["files"], "generated files")
    _exact_keys(files_raw, expected_paths, "generated files")
    files = {
        path: _nonempty_string(files_raw[path], f"generated file {path}")
        for path in sorted(expected_paths)
    }
    if any(
        PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts
        for path in files
    ):
        raise SkillCompilerError("generated file paths must remain package-relative")
    _validate_pyproject(files["pyproject.toml"], name, import_name)
    test_error = _meaningful_test_error(
        files[f"tests/test_{import_name}.py"], import_name
    )
    if test_error is not None:
        raise SkillCompilerError(test_error)
    return GeneratedPackage(name=name, reflection=reflection, files=files)


def _read_telemetry_tools(path: Path, session_id: str) -> list[str]:
    if not path.is_file():
        raise SkillCompilerError(f"telemetry stream is unavailable: {path}")
    tools: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SkillCompilerError(
            f"cannot read telemetry stream {path}: {error}"
        ) from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = TelemetryEvent.from_json_line(line)
        except TelemetryValidationError as error:
            raise SkillCompilerError(
                f"telemetry stream {path}:{line_number} is invalid: {error}"
            ) from error
        if event.session_id != session_id:
            raise SkillCompilerError("telemetry stream contains another session")
        if (
            event.kind == "tool"
            and event.tool is not None
            and not event.tool.is_error
            and event.tool.name != "compile_skill"
        ):
            tools.append(event.tool.name)
    return tools


def _repeated_sequence(tools: Sequence[str]) -> tuple[tuple[str, ...], int]:
    candidates: list[tuple[int, int, tuple[str, ...]]] = []
    for length in range(
        MIN_REPEATED_SEQUENCE_LENGTH,
        min(MAX_REPEATED_SEQUENCE_LENGTH, len(tools)) + 1,
    ):
        counts = Counter(
            tuple(tools[index : index + length])
            for index in range(len(tools) - length + 1)
        )
        candidates.extend(
            (length * count, count, sequence)
            for sequence, count in counts.items()
            if count >= 2
        )
    if not candidates:
        raise SkillCompilerError(
            "telemetry contains no repeated tool sequence of at least two steps"
        )
    _, count, sequence = max(
        candidates,
        key=lambda item: (item[0], len(item[2]), item[1], item[2]),
    )
    return sequence, count


def mine_workflow(
    trajectory: SessionTrajectory,
    settings: SynthSettings,
) -> str:
    telemetry_path = settings.telemetry.directory / (
        f"{_safe_session_id(trajectory.session_id)}.jsonl"
    )
    sequence, count = _repeated_sequence(
        _read_telemetry_tools(telemetry_path, trajectory.session_id)
    )
    return (
        f"Recurring tool sequence ({count} occurrences): "
        f"{' -> '.join(sequence)}. Compile the reusable multi-step workflow and "
        "generalize task-specific arguments."
    )


def _generation_prompt(
    trajectory: SessionTrajectory,
    workflow: str,
    reason: str,
    name_hint: str | None,
) -> str:
    bounded_workflow = workflow.strip()[:MAX_WORKFLOW_CHARACTERS]
    bounded_trajectory = trajectory.trajectory[-MAX_TRAJECTORY_CHARACTERS:]
    return (
        f"Task: {trajectory.task_query}\n\n"
        f"Why compilation was requested: {reason.strip()}\n\n"
        f"Preferred name: {name_hint or 'choose a general name'}\n\n"
        f"Recurring workflow:\n{bounded_workflow}\n\n"
        f"Trajectory evidence:\n{bounded_trajectory}"
    )


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_audit(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SkillCompilerError(f"cannot read skill audit log: {error}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError as error:
            raise SkillCompilerError(
                f"skill audit log line {line_number} is invalid JSON"
            ) from error
        record = _mapping(value, f"skill audit line {line_number}")
        if record.get("schemaVersion") != AUDIT_SCHEMA_VERSION:
            raise SkillCompilerError(
                f"skill audit log line {line_number} has an unknown schema"
            )
        records.append(record)
    return records


def _append_audit(path: Path, record: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "timestamp": _timestamp(),
        **record,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{encoded}\n")
        stream.flush()
        os.fsync(stream.fileno())


def _request_record(
    request: CompileRequest,
    trajectory: SessionTrajectory,
    workflow: str,
) -> dict[str, object]:
    return {
        "action": "compile_started",
        "requestId": request.request_id,
        "sessionId": trajectory.session_id,
        "actor": request.actor,
        "reason": request.reason,
        "nameHint": request.name_hint,
        "sourceSession": request.session_path.resolve().as_posix(),
        "workflow": workflow,
        "workflowDigest": _workflow_digest(workflow),
    }


def _gate_policy(settings: SynthSettings) -> GatePolicy:
    return GatePolicy(
        repository_root=settings.workspace,
        workspace_roots=(settings.workspace,),
        bundled_skills_dir=(
            settings.workspace / "packages" / "coding-agent" / "skills"
        ),
        uv_cache_dir=settings.workspace / GATE_UV_CACHE,
        python_version="3.11",
        offline=True,
    )


def _write_candidate(root: Path, package: GeneratedPackage) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / package.name
    try:
        candidate.mkdir()
    except FileExistsError as error:
        raise SkillCompilerError(
            f"quarantine candidate already exists: {candidate}"
        ) from error
    try:
        for relative, content in package.files.items():
            destination = candidate / PurePosixPath(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
    except Exception:
        shutil.rmtree(candidate, ignore_errors=True)
        raise
    return candidate


def _remove_runtime_cache_artifacts(root: Path) -> tuple[str, ...]:
    removed: list[str] = []
    for directory, directory_names, file_names in os.walk(
        root, topdown=False, followlinks=False
    ):
        current = Path(directory)
        for name in file_names:
            path = current / name
            if path.suffix not in RUNTIME_CACHE_SUFFIXES:
                continue
            removed.append(path.relative_to(root).as_posix())
            path.unlink()
        for name in directory_names:
            if name not in RUNTIME_CACHE_DIRECTORIES:
                continue
            path = current / name
            removed.append(path.relative_to(root).as_posix())
            if path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path)
    return tuple(sorted(removed))


def _fixture_response(path: Path) -> str:
    try:
        return path.expanduser().resolve().read_text(encoding="utf-8")
    except OSError as error:
        raise SkillCompilerError(
            f"cannot read skill fixture {path}: {error}"
        ) from error


def _fixture_package(path: Path) -> GeneratedPackage:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise SkillCompilerError("skill fixture directory must not be a symlink")
    try:
        root = expanded.resolve(strict=True)
    except OSError as error:
        raise SkillCompilerError(
            f"cannot resolve skill fixture {path}: {error}"
        ) from error
    if not root.is_dir():
        raise SkillCompilerError(f"skill fixture is not a directory: {path}")
    name = _validate_name(root.name)
    import_name = name.replace("-", "_")
    expected_paths = {
        "SKILL.md",
        "pyproject.toml",
        f"src/{import_name}/__init__.py",
        f"tests/test_{import_name}.py",
    }
    files: dict[str, str] = {}
    for entry in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if entry.is_symlink():
            raise SkillCompilerError(
                f"skill fixture contains a symlink: {entry.relative_to(root)}"
            )
        if not entry.is_file():
            continue
        relative = entry.relative_to(root).as_posix()
        if relative not in expected_paths:
            raise SkillCompilerError(
                f"skill fixture contains an unexpected file: {relative}"
            )
        try:
            files[relative] = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise SkillCompilerError(
                f"cannot read skill fixture file {relative}: {error}"
            ) from error
    missing = expected_paths.difference(files)
    if missing:
        raise SkillCompilerError(
            "skill fixture is missing: " + ", ".join(sorted(missing))
        )
    return GeneratedPackage(
        name=name,
        reflection=f"Reviewed deterministic gate fixture for {name}",
        files=files,
    )


def _model_response(
    settings: SynthSettings,
    prompt: str,
) -> str:
    endpoint = settings.endpoint
    if endpoint is None:
        raise SkillCompilerError("endpoint is required for skill generation")
    try:
        return local_model_callable(settings, model_id=endpoint.model)(
            GENERATION_SYSTEM_PROMPT,
            prompt,
        )
    except ModelCallError as error:
        raise SkillCompilerError(str(error)) from error


def _generate_package(
    settings: SynthSettings,
    prompt: str,
    fixture_path: Path | None,
) -> GeneratedPackage:
    if fixture_path is not None:
        if fixture_path.expanduser().is_dir():
            return _fixture_package(fixture_path)
        return parse_generated_package(_fixture_response(fixture_path))
    return parse_generated_package(_model_response(settings, prompt))


class SkillCompiler:
    def compile(self, request: CompileRequest) -> CompileOutcome:
        _validate_request_id(request.request_id)
        trajectory = load_session_trajectory(
            request.session_path, wait_for_stability=True
        )
        settings = load_synth_settings(trajectory.workspace)
        if request.name_hint is not None:
            _validate_name(request.name_hint)
        configured_workflow = request.workflow.strip() if request.workflow else ""
        audit_path = settings.workspace / AUDIT_PATH
        lock_path = settings.workspace / LOCK_PATH
        if settings.kill_switch or not settings.skill_compiler.enabled:
            disabled_workflow = (
                configured_workflow or "disabled before telemetry mining"
            )
            with _exclusive_lock(lock_path):
                _append_audit(
                    audit_path,
                    {
                        **_request_record(request, trajectory, disabled_workflow),
                        "action": "compile_disabled",
                    },
                )
            raise SkillCompilerError("dynamic skill compilation is disabled")
        workflow = configured_workflow or mine_workflow(trajectory, settings)
        if not workflow or len(workflow) > MAX_WORKFLOW_CHARACTERS:
            raise SkillCompilerError(
                f"workflow must contain 1 to {MAX_WORKFLOW_CHARACTERS} characters"
            )
        with _exclusive_lock(lock_path):
            records = _read_audit(audit_path)
            started_ids = {
                str(record.get("requestId"))
                for record in records
                if record.get("action") == "compile_started"
                and record.get("sessionId") == trajectory.session_id
            }
            if request.request_id in started_ids:
                raise SkillCompilerError(
                    f"compile request {request.request_id} is already recorded"
                )
            if len(started_ids) >= settings.skill_compiler.max_per_session:
                _append_audit(
                    audit_path,
                    {
                        **_request_record(request, trajectory, workflow),
                        "action": "compile_cap_rejected",
                        "limit": settings.skill_compiler.max_per_session,
                    },
                )
                raise SkillCompilerError(
                    "dynamic skill compilation cap is exhausted for this session"
                )
            _append_audit(
                audit_path,
                _request_record(request, trajectory, workflow),
            )
            package: GeneratedPackage | None = None
            candidate: Path | None = None
            try:
                package = _generate_package(
                    settings,
                    _generation_prompt(
                        trajectory,
                        workflow,
                        request.reason,
                        request.name_hint,
                    ),
                    request.fixture_path,
                )
                candidate = _write_candidate(
                    settings.skill_compiler.quarantine_path,
                    package,
                )
                gate = evaluate(candidate, _gate_policy(settings))
                terminal_base: dict[str, object] = {
                    "requestId": request.request_id,
                    "sessionId": trajectory.session_id,
                    "actor": request.actor,
                    "reason": request.reason,
                    "name": package.name,
                    "reflection": package.reflection,
                    "sourceSession": request.session_path.resolve().as_posix(),
                    "workflowDigest": _workflow_digest(workflow),
                    "contentHash": gate.content_hash,
                    "gate": gate.to_dict(),
                }
                if not gate.passed:
                    _append_audit(
                        audit_path,
                        {"action": "rejected", **terminal_base},
                    )
                    return CompileOutcome(
                        request_id=request.request_id,
                        session_id=trajectory.session_id,
                        name=package.name,
                        status="rejected",
                        content_hash=gate.content_hash,
                        destination=candidate,
                        gate=gate,
                        workflow=workflow,
                    )
                destination = (
                    settings.workspace / ".prime" / "agent" / "skills" / package.name
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() or destination.is_symlink():
                    raise SkillCompilerError(
                        f"skill destination already exists: {destination}"
                    )
                if content_hash(candidate) != gate.content_hash:
                    raise SkillCompilerError(
                        "quarantine candidate changed after gate evaluation"
                    )
                candidate.rename(destination)
                if content_hash(destination) != gate.content_hash:
                    destination.rename(candidate)
                    raise SkillCompilerError("promoted skill hash changed during move")
                try:
                    _append_audit(
                        audit_path,
                        {
                            "action": "promoted",
                            **terminal_base,
                            "destination": destination.as_posix(),
                        },
                    )
                except Exception:
                    destination.rename(candidate)
                    raise
                return CompileOutcome(
                    request_id=request.request_id,
                    session_id=trajectory.session_id,
                    name=package.name,
                    status="promoted",
                    content_hash=gate.content_hash,
                    destination=destination,
                    gate=gate,
                    workflow=workflow,
                )
            except Exception as error:
                _append_audit(
                    audit_path,
                    {
                        "action": "failed",
                        "requestId": request.request_id,
                        "sessionId": trajectory.session_id,
                        "actor": request.actor,
                        "reason": request.reason,
                        "name": package.name if package is not None else None,
                        "sourceSession": request.session_path.resolve().as_posix(),
                        "workflowDigest": _workflow_digest(workflow),
                        "contentHash": (
                            content_hash(candidate)
                            if candidate is not None and candidate.exists()
                            else None
                        ),
                        "error": str(error),
                    },
                )
                if isinstance(error, SkillCompilerError):
                    raise
                raise SkillCompilerError(str(error)) from error

    def demote(
        self,
        workspace: Path,
        name: str,
        *,
        actor: str,
        reason: str,
    ) -> DemotionOutcome:
        settings = load_synth_settings(workspace.resolve())
        validated_name = _validate_name(name)
        audit_path = settings.workspace / AUDIT_PATH
        with _exclusive_lock(settings.workspace / LOCK_PATH):
            records = _read_audit(audit_path)
            active_promotion: dict[str, object] | None = None
            for record in records:
                if record.get("name") != validated_name:
                    continue
                if record.get("action") == "promoted":
                    active_promotion = record
                elif record.get("action") == "demoted":
                    active_promotion = None
            if active_promotion is None:
                raise SkillCompilerError(
                    f"skill {validated_name!r} has no active compiler promotion receipt"
                )
            expected_hash = active_promotion.get("contentHash")
            if not isinstance(expected_hash, str):
                raise SkillCompilerError("promotion receipt has no content hash")
            source = settings.workspace / ".prime" / "agent" / "skills" / validated_name
            if not source.is_dir() or source.is_symlink():
                raise SkillCompilerError(f"promoted skill is unavailable: {source}")
            removed_runtime_artifacts = _remove_runtime_cache_artifacts(source)
            current_hash = content_hash(source)
            if current_hash != expected_hash:
                _append_audit(
                    audit_path,
                    {
                        "action": "demotion_hash_rejected",
                        "name": validated_name,
                        "actor": actor,
                        "reason": reason,
                        "expectedHash": expected_hash,
                        "contentHash": current_hash,
                        "removedRuntimeArtifacts": removed_runtime_artifacts,
                    },
                )
                raise SkillCompilerError(
                    "promoted skill changed after its gate; refusing demotion"
                )
            destination_root = settings.workspace / DEMOTED_PATH / validated_name
            destination_root.mkdir(parents=True, exist_ok=True)
            destination = destination_root / _filesystem_timestamp()
            source.rename(destination)
            try:
                _append_audit(
                    audit_path,
                    {
                        "action": "demoted",
                        "name": validated_name,
                        "actor": actor,
                        "reason": reason,
                        "contentHash": current_hash,
                        "destination": destination.as_posix(),
                        "removedRuntimeArtifacts": removed_runtime_artifacts,
                    },
                )
            except Exception:
                destination.rename(source)
                raise
            return DemotionOutcome(
                name=validated_name,
                content_hash=current_hash,
                destination=destination,
            )
