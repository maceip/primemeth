from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import yaml  # type: ignore[import-untyped]

from optimizer.transport import StubPrimeAgentTransport
from optimizer.vendor.autosaddler_v2.core.domain import (
    ArtifactRef,
    Candidate,
    Case,
    Cost,
    Evaluation,
    JsonValue,
    Observation,
    Split,
    canonical_json,
    sha256_digest,
)
from optimizer.vendor.autosaddler_v2.core.ports import (
    EvaluationContext,
    ScenarioComponents,
)
from optimizer.vendor.autosaddler_v2.harness.git import (
    GitHarnessSpace,
    GitVerificationContext,
    GitVerificationVerdict,
)
from optimizer.vendor.autosaddler_v2.prompting.history import build_history_bundle
from optimizer.vendor.autosaddler_v2.prompting.models import SessionSpec
from optimizer.vendor.autosaddler_v2.providers.base import AgentTransport
from optimizer.vendor.autosaddler_v2.providers.workspace_renderer import RenderedSession
from optimizer.vendor.autosaddler_v2.storage.local import LocalRunStore

TARGET_SKILL_PATH = ".prime/agent/skills/synth-harness/SKILL.md"
WRITABLE_PATHS = (
    PurePosixPath(".prime/agent/skills"),
    PurePosixPath(".prime/agent/extensions/synth"),
    PurePosixPath("optimizer/overlays/prompts"),
    PurePosixPath("optimizer/overlays/configs"),
)
FORBIDDEN_PATHS = (
    PurePosixPath("packages"),
    PurePosixPath("prime-agent-runtime"),
    PurePosixPath("tests"),
    PurePosixPath("eval"),
    PurePosixPath("evals"),
    PurePosixPath("data"),
)
PYTEST_REQUIREMENT = "pytest>=8.0.0"


@dataclass(frozen=True, slots=True)
class HarnessTask:
    case: Case
    root: Path
    prompt: str
    solution: Mapping[str, str]
    digest: str


@dataclass(frozen=True, slots=True)
class SynthHarnessSettings:
    source_repo: Path
    base_revision: str
    train_root: Path
    development_root: Path
    train_case_ids: tuple[str, ...]
    development_case_ids: tuple[str, ...]
    seed_skill_file: Path
    capability_phase_iterations: int
    evaluation_timeout_seconds: float


class SynthHarnessVerifier:
    def __init__(self, *, case_ids: Sequence[str], tsx_path: Path) -> None:
        self.case_ids = tuple(case_ids)
        self.tsx_path = tsx_path.resolve()
        if not self.tsx_path.is_file():
            raise FileNotFoundError(
                f"Synth harness verifier requires tsx at {self.tsx_path}"
            )

    def __call__(self, context: GitVerificationContext) -> GitVerificationVerdict:
        patch = _verification_patch(context)
        leaked_ids = anti_cheating_scan(patch, self.case_ids)
        text_errors = _text_change_errors(context)
        commands = (
            (
                "ruff check",
                sys.executable,
                "-m",
                "ruff",
                "check",
                str(context.workspace / "optimizer"),
                str(context.workspace / "tests/harness_regression"),
                "--exclude",
                "optimizer/vendor",
            ),
            (
                "pytest tests/harness_regression",
                sys.executable,
                "-m",
                "pytest",
                str(context.workspace / "tests/harness_regression"),
                "-q",
                "-p",
                "no:cacheprovider",
                "--rootdir",
                str(context.workspace),
            ),
        )
        command_failures: list[str] = []
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["SYNTH_TEST_TSX"] = str(self.tsx_path)
        environment["UV_PROJECT_ENVIRONMENT"] = sys.prefix
        for label, *command in commands:
            result = subprocess.run(
                command,
                cwd=context.workspace,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stdout + result.stderr).strip()[-2_000:]
                command_failures.append(
                    f"{label} exited {result.returncode}: {detail}"
                )
        failures = [
            *(
                f"evaluation case ID leaked into patch: {case_id}"
                for case_id in leaked_ids
            ),
            *text_errors,
            *command_failures,
        ]
        return GitVerificationVerdict(
            accepted=not failures,
            check="synth-harness-verifier/v1",
            summary="all verifier gates passed"
            if not failures
            else "; ".join(failures),
            artifacts=(),
        )


class SynthHarnessEvaluator:
    def __init__(
        self,
        *,
        harness_space: GitHarnessSpace,
        tasks: Mapping[str, HarnessTask],
        transport: AgentTransport,
        artifact_root: Path,
        timeout_seconds: float,
        fingerprint: str,
        seed_skill_file: Path,
    ) -> None:
        self.harness_space = harness_space
        self.tasks = dict(tasks)
        self.transport = transport
        self.artifact_root = artifact_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.fingerprint = fingerprint
        self.seed_skill_file = seed_skill_file

    async def evaluate(
        self,
        candidate: Candidate,
        cases: Sequence[Case],
        context: EvaluationContext,
    ) -> Evaluation:
        if not cases or any(case.split != context.split for case in cases):
            raise ValueError(
                "Synth harness evaluator cases must match the context split"
            )
        observations: list[Observation] = []
        pending: list[tuple[Case, int]] = []
        for case in cases:
            for repetition in range(context.repetitions):
                completed = context.attempt_sink.completed(
                    candidate_id=candidate.candidate_id,
                    case_id=case.case_id,
                    repetition=repetition,
                )
                if completed is not None:
                    observations.append(completed)
                else:
                    pending.append((case, repetition))

        materialized = (
            self.harness_space.materialize(candidate, "evaluate") if pending else None
        )
        try:
            for case, repetition in pending:
                attempt_id, attempt_number = context.attempt_sink.start(
                    candidate_id=candidate.candidate_id,
                    case_id=case.case_id,
                    repetition=repetition,
                )
                try:
                    assert materialized is not None
                    observation = await self._evaluate_one(
                        candidate=candidate,
                        candidate_root=materialized.root,
                        case=case,
                        repetition=repetition,
                        attempt_number=attempt_number,
                        artifact_dir=context.artifact_dir,
                    )
                except Exception as error:
                    context.attempt_sink.fail(
                        attempt_id,
                        type(error).__name__,
                        Cost(rollouts=1),
                    )
                    raise
                context.attempt_sink.complete(attempt_id, observation, Cost(rollouts=1))
                observations.append(observation)
        finally:
            if materialized is not None:
                materialized.release()

        observations.sort(key=lambda item: (item.case_id, item.repetition))
        evaluation_id = sha256_digest(context.operation_id)
        return Evaluation(
            evaluation_id=evaluation_id,
            candidate_id=candidate.candidate_id,
            split=context.split,
            purpose=context.purpose,
            iteration=context.iteration,
            requested_case_ids=tuple(case.case_id for case in cases),
            observations=tuple(observations),
            artifact_dir=ArtifactRef(
                uri=context.artifact_dir.relative_to(self.artifact_root).as_posix(),
                kind="synth-harness-evaluation-directory",
            ),
        )

    async def _evaluate_one(
        self,
        *,
        candidate: Candidate,
        candidate_root: Path,
        case: Case,
        repetition: int,
        attempt_number: int,
        artifact_dir: Path,
    ) -> Observation:
        task = self.tasks[case.case_id]
        workspace = artifact_dir / "workspaces" / f"{case.case_id}-{repetition}"
        if workspace.exists():
            shutil.rmtree(workspace)
        shutil.copytree(
            task.root, workspace, ignore=shutil.ignore_patterns("task.yaml")
        )
        _copy_candidate_overlays(candidate_root, workspace)
        target_skill = workspace / TARGET_SKILL_PATH
        if not target_skill.is_file():
            target_skill.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.seed_skill_file, target_skill)
        session_id = sha256_digest(
            canonical_json(
                {
                    "candidate_id": candidate.candidate_id,
                    "case_id": case.case_id,
                    "repetition": repetition,
                }
            )
        )
        if isinstance(self.transport, StubPrimeAgentTransport):
            self.transport.prepare_fixture(session_id, task.solution)
        autosaddler_dir = workspace / ".autosaddler"
        autosaddler_dir.mkdir(parents=True, exist_ok=True)
        instruction_path = autosaddler_dir / "task-instructions.md"
        instruction_path.write_text(
            "Repair the repository task, run its tests, and keep changes scoped "
            "to the task.",
            encoding="utf-8",
        )
        rendered = RenderedSession(
            provider="prime-agent",
            workspace=workspace,
            task_prompt=task.prompt,
            system_context=instruction_path.read_text(encoding="utf-8"),
            instruction_path=instruction_path,
            skill_directory=workspace / ".prime/agent/skills",
            output_schema_path=autosaddler_dir / "task-output-schema.json",
            output_path=autosaddler_dir / "task-output.json",
            allowed_tools=(),
            session_id=session_id,
            trace_dir=artifact_dir / "agent-traces" / f"{case.case_id}-{repetition}",
        )
        transport_outcome = await asyncio.wait_for(
            self.transport.run(rendered, self.timeout_seconds),
            timeout=self.timeout_seconds,
        )
        test_result = await _run_fixture_tests(workspace, self.timeout_seconds)
        result_path = artifact_dir / "results" / f"{case.case_id}-{repetition}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            canonical_json(
                {
                    "schema_version": "synth-harness-task-result/v1",
                    "case_id": case.case_id,
                    "agent_transcript": transport_outcome.raw_response,
                    "pytest_exit_code": test_result.returncode,
                    "pytest_stdout": test_result.stdout,
                    "pytest_stderr": test_result.stderr,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        passed = test_result.returncode == 0
        payload = result_path.read_bytes()
        return Observation.create(
            candidate_id=candidate.candidate_id,
            case_id=case.case_id,
            split=case.split,
            repetition=repetition,
            disposition="success" if passed else "task_failure",
            score=1.0 if passed else 0.0,
            evaluator_fingerprint=self.fingerprint,
            output=ArtifactRef(
                uri=result_path.relative_to(self.artifact_root).as_posix(),
                kind="synth-harness-task-result",
                sha256=sha256_digest(payload),
                bytes=len(payload),
            ),
            attempts=attempt_number,
            cost=Cost(rollouts=1),
            metadata={"task_digest": task.digest},
        )


class SynthHarnessEvidenceBuilder:
    def __init__(self, store: LocalRunStore) -> None:
        self.store = store

    def build(self, evaluation: Evaluation) -> ArtifactRef:
        if evaluation.split != "train":
            raise ValueError("Synth optimization evidence must come from training")
        evidence_id = sha256_digest(evaluation.evaluation_id)
        return self.store.write_json(
            f"evidence/{evidence_id.removeprefix('sha256:')}/evidence.json",
            {
                "schema_version": "synth-harness-evidence/v1",
                "evaluation_id": evaluation.evaluation_id,
                "observations": [
                    {
                        "case_id": item.case_id,
                        "disposition": item.disposition,
                        "score": item.score,
                        "output": item.output.uri if item.output is not None else None,
                    }
                    for item in evaluation.observations
                ],
            },
            kind="synth-harness-training-evidence",
        )


class SynthHarnessPromptPack:
    def __init__(
        self,
        *,
        store: LocalRunStore,
        capability_phase_iterations: int,
    ) -> None:
        self.store = store
        self.capability_phase_iterations = capability_phase_iterations

    def session(
        self,
        kind: str,
        context: Mapping[str, JsonValue],
    ) -> SessionSpec:
        iteration = context.get("iteration", 0)
        phase = (
            "capability"
            if isinstance(iteration, int)
            and iteration < self.capability_phase_iterations
            else "steering"
        )
        session_context = {**context, "session_kind": kind, "phase": phase}
        history = build_history_bundle(self.store, session_context)
        if kind == "evolve":
            schema = _object_schema(
                ("schema_version", "parent_ids", "component_sources", "rationale")
            )
            task_prompt = (
                "Select accepted parent lineage for the next matched training batch."
            )
        elif kind == "diagnose_patch":
            schema = _object_schema(("schema_version", "changed_paths", "diagnosis"))
            task_prompt = (
                "Diagnose the failing training traces and improve only the "
                "allowlisted Synth skill, steering extension, or overlay "
                "text/config assets. Do not mention evaluation case IDs in "
                "changed files."
            )
        elif kind == "reflect":
            schema = _object_schema(("schema_version", "lessons"))
            task_prompt = (
                "Record a compact lesson supported only by the matched training cases."
            )
        else:
            raise ValueError(f"Unknown Synth harness session kind: {kind}")
        workspace_files = {
            ".autosaddler/session_context.json": canonical_json(session_context) + "\n",
            **history.workspace_files,
        }
        return SessionSpec(
            kind=kind,
            system_context=(
                "Optimize the Prime Agent harness through the declared structured "
                "contract. Development payloads are quarantined and candidate "
                "changes must remain scoped."
            ),
            task_prompt=task_prompt,
            skills={"synth-optimizer": _optimizer_skill()},
            output_schema=schema,
            workspace_files=workspace_files,
            capabilities=frozenset(
                {"read_workspace", "edit_workspace", "run_commands", "load_skills"}
            ),
            mutation_label=phase if kind == "diagnose_patch" else None,
        )


def build_synth_harness_components(
    *,
    settings: SynthHarnessSettings,
    run_dir: Path,
    store: LocalRunStore,
    transport: AgentTransport,
) -> ScenarioComponents:
    train_tasks = _load_tasks(
        settings.train_root,
        "train",
        settings.train_case_ids,
    )
    development_tasks = _load_tasks(
        settings.development_root,
        "development",
        settings.development_case_ids,
    )
    all_tasks = {**train_tasks, **development_tasks}
    verifier = SynthHarnessVerifier(
        case_ids=tuple(all_tasks),
        tsx_path=settings.source_repo / "node_modules" / ".bin" / "tsx",
    )
    harness = GitHarnessSpace(
        source_repo=settings.source_repo,
        base_revision=settings.base_revision,
        store_root=run_dir / "candidates",
        worktree_root=run_dir / "worktrees",
        writable_paths=WRITABLE_PATHS,
        forbidden_paths=FORBIDDEN_PATHS,
        verifier=verifier,
    )
    task_digest = sha256_digest(
        canonical_json(
            {case_id: task.digest for case_id, task in sorted(all_tasks.items())}
        )
    )
    evaluator_fingerprint = f"synth-harness/v1:{task_digest}"
    return ScenarioComponents(
        name="synth_harness",
        version="1",
        harness_space=harness,
        evaluator=SynthHarnessEvaluator(
            harness_space=harness,
            tasks=all_tasks,
            transport=transport,
            artifact_root=run_dir,
            timeout_seconds=settings.evaluation_timeout_seconds,
            fingerprint=evaluator_fingerprint,
            seed_skill_file=settings.seed_skill_file,
        ),
        evidence_builder=SynthHarnessEvidenceBuilder(store),
        prompt_pack=SynthHarnessPromptPack(
            store=store,
            capability_phase_iterations=settings.capability_phase_iterations,
        ),
        train_cases=tuple(task.case for task in train_tasks.values()),
        development_cases=tuple(task.case for task in development_tasks.values()),
        required_capabilities=frozenset(
            {"read_workspace", "edit_workspace", "run_commands", "load_skills"}
        ),
        resolved_entities={
            "resolved/sources/harness.json": {
                "type": "git",
                "source_repo": str(settings.source_repo),
                "base_revision": harness.base_revision,
                "writable_paths": [str(path) for path in WRITABLE_PATHS],
                "forbidden_paths": [str(path) for path in FORBIDDEN_PATHS],
            },
            "resolved/sources/tasks.json": {
                "train_case_ids": list(train_tasks),
                "development_case_ids": list(development_tasks),
                "content_digest": task_digest,
                "seed_skill_sha256": sha256_digest(
                    settings.seed_skill_file.read_bytes()
                ),
                "test": {"state": "opaque", "opened": False},
            },
            "resolved/sources/evaluator.json": {
                "fingerprint": evaluator_fingerprint,
                "attempt_protocol": "completed-before-start",
                "score": "pytest binary outcome",
            },
            "resolved/sources/mutation-policy.json": {
                "anti_cheating": "reject added evaluation case IDs",
                "steering": "UTF-8 text only",
                "verification": ["ruff", "pytest tests/harness_regression"],
            },
        },
    )


def parse_synth_harness_settings(
    value: Mapping[str, JsonValue],
    *,
    base_dir: Path,
) -> SynthHarnessSettings:
    expected = {
        "source_repo",
        "base_revision",
        "train_root",
        "development_root",
        "train_case_ids",
        "development_case_ids",
        "seed_skill_file",
        "capability_phase_iterations",
        "evaluation_timeout_seconds",
    }
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing or extra:
        raise ValueError(
            f"Invalid Synth harness settings: missing={missing}, extra={extra}"
        )
    capability_iterations = value["capability_phase_iterations"]
    if (
        isinstance(capability_iterations, bool)
        or not isinstance(capability_iterations, int)
        or capability_iterations < 0
    ):
        raise TypeError("capability_phase_iterations must be a nonnegative integer")
    timeout = value["evaluation_timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise TypeError("evaluation_timeout_seconds must be positive")
    return SynthHarnessSettings(
        source_repo=_directory(base_dir, value["source_repo"], "source_repo"),
        base_revision=_string(value["base_revision"], "base_revision"),
        train_root=_directory(base_dir, value["train_root"], "train_root"),
        development_root=_directory(
            base_dir,
            value["development_root"],
            "development_root",
        ),
        train_case_ids=_string_list(value["train_case_ids"], "train_case_ids"),
        development_case_ids=_string_list(
            value["development_case_ids"],
            "development_case_ids",
        ),
        seed_skill_file=_file(base_dir, value["seed_skill_file"], "seed_skill_file"),
        capability_phase_iterations=capability_iterations,
        evaluation_timeout_seconds=float(timeout),
    )


def anti_cheating_scan(patch: str, case_ids: Sequence[str]) -> tuple[str, ...]:
    added_text = "\n".join(
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    return tuple(sorted(case_id for case_id in case_ids if case_id in added_text))


def _verification_patch(context: GitVerificationContext) -> str:
    patch = subprocess.run(
        ["git", "diff", "--no-ext-diff", "--unified=0", "HEAD", "--"],
        cwd=context.workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    added: list[str] = []
    for path in context.changed_paths:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", str(path)],
            cwd=context.workspace,
            capture_output=True,
            check=False,
        )
        destination = context.workspace.joinpath(*path.parts)
        if tracked.returncode == 0 or not destination.is_file():
            continue
        try:
            text = destination.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        added.extend(f"+{line}" for line in text.splitlines())
    return patch + "\n" + "\n".join(added)


def _text_change_errors(context: GitVerificationContext) -> tuple[str, ...]:
    errors: list[str] = []
    for path in context.changed_paths:
        check_text = context.patch_label == "steering" or path.is_relative_to(
            PurePosixPath(".prime/agent/extensions/synth")
        )
        if not check_text:
            continue
        destination = context.workspace.joinpath(*path.parts)
        if not destination.exists():
            continue
        try:
            content = destination.read_bytes()
            content.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            errors.append(f"steering change is not UTF-8 text: {path}")
            continue
        if b"\0" in content:
            errors.append(f"steering change contains NUL bytes: {path}")
    return tuple(errors)


def _load_tasks(
    root: Path,
    split: str,
    selected_ids: Sequence[str],
) -> dict[str, HarnessTask]:
    selected = set(selected_ids)
    loaded: dict[str, HarnessTask] = {}
    for manifest_path in sorted(root.glob("*/task.yaml")):
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise TypeError(f"Task manifest must be an object: {manifest_path}")
        case_id = _string(cast(JsonValue, raw.get("id")), "task.id")
        if case_id not in selected:
            continue
        if raw.get("split") != split:
            raise ValueError(f"Task split mismatch for {case_id}")
        prompt = _string(cast(JsonValue, raw.get("prompt")), "task.prompt")
        solution_value = raw.get("solution")
        if not isinstance(solution_value, Mapping) or any(
            not isinstance(path, str) or not isinstance(content, str)
            for path, content in solution_value.items()
        ):
            raise TypeError(f"Task solution must map paths to text: {case_id}")
        task_root = manifest_path.parent
        digest = _tree_digest(task_root)
        loaded[case_id] = HarnessTask(
            case=Case(
                case_id=case_id,
                split=cast(Split, split),
                payload={
                    "fixture": task_root.relative_to(root).as_posix(),
                    "digest": digest,
                },
            ),
            root=task_root,
            prompt=prompt,
            solution=cast(Mapping[str, str], solution_value),
            digest=digest,
        )
    missing = sorted(selected - loaded.keys())
    if missing:
        raise ValueError(f"Selected fixture tasks are missing: {missing}")
    return {case_id: loaded[case_id] for case_id in selected_ids}


def _tree_digest(root: Path) -> str:
    records = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_digest(path.read_bytes()),
            }
        )
    return sha256_digest(canonical_json(records))


def _copy_candidate_overlays(candidate_root: Path, workspace: Path) -> None:
    for relative in WRITABLE_PATHS:
        source = candidate_root.joinpath(*relative.parts)
        if not source.is_dir():
            continue
        destination = workspace.joinpath(*relative.parts)
        shutil.copytree(source, destination, dirs_exist_ok=True)


async def _run_fixture_tests(
    workspace: Path,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = await asyncio.create_subprocess_exec(
        *_uv_run_prefix(PYTEST_REQUIREMENT),
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        cwd=workspace,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return subprocess.CompletedProcess(
        args=["uv", "run", "pytest"],
        returncode=process.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )


def _uv_run_prefix(*requirements: str) -> tuple[str, ...]:
    uv_binary = shutil.which("uv")
    if uv_binary is None:
        raise RuntimeError("Synth harness execution requires uv")
    command = [
        uv_binary,
        "run",
        "--isolated",
        "--no-project",
        "--managed-python",
        "--python",
        "3.12",
    ]
    for requirement in requirements:
        command.extend(("--with", requirement))
    return tuple(command)


def _object_schema(required: tuple[str, ...]) -> Mapping[str, JsonValue]:
    return {
        "type": "object",
        "required": list(required),
        "properties": {name: {} for name in required},
        "additionalProperties": False,
    }


def _optimizer_skill() -> str:
    return (
        "---\n"
        "name: synth-optimizer\n"
        'description: "Diagnose and improve the scoped Synth agent harness."\n'
        "---\n\n"
        "# Synth optimizer\n\n"
        "Use training evidence, make the smallest scoped change, and report exact "
        "paths.\n"
    )


def _directory(base_dir: Path, value: JsonValue, label: str) -> Path:
    raw = _string(value, label)
    path = Path(raw)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve(strict=True)


def _file(base_dir: Path, value: JsonValue, label: str) -> Path:
    path = _directory(base_dir, value, label)
    if not path.is_file():
        raise FileNotFoundError(f"{label} must be a file: {path}")
    return path


def _string(value: JsonValue, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be a non-empty string")
    return value


def _string_list(value: JsonValue, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise TypeError(f"{label} must be a non-empty list of strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} must contain unique values")
    return tuple(cast(list[str], value))
