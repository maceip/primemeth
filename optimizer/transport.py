from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from optimizer.vendor.autosaddler_v2.core.domain import (
    JsonValue,
    canonical_json,
)
from optimizer.vendor.autosaddler_v2.prompting.models import Capability, Usage
from optimizer.vendor.autosaddler_v2.providers.base import (
    AgentTransport,
    BaseAgentProvider,
    TransportOutcome,
)
from optimizer.vendor.autosaddler_v2.providers.workspace_renderer import (
    RenderedSession,
    WorkspaceRenderer,
)
from synth.control_plane import (
    MANAGED_WORKSPACE_ENV,
    require_control_plane_protected,
)

PRIME_CAPABILITY_TO_TOOLS: Mapping[Capability, tuple[str, ...]] = MappingProxyType(
    {
        "read_workspace": (),
        "edit_workspace": (),
        "run_commands": (),
        "load_skills": (),
        "network": (),
    }
)


def prime_agent_renderer() -> WorkspaceRenderer:
    return WorkspaceRenderer(
        provider="prime-agent",
        instruction_file=".autosaddler/prime-agent-instructions.md",
        skill_directory=".autosaddler/prime-agent-skills",
        capability_tools=PRIME_CAPABILITY_TO_TOOLS,
    )


class PrimeAgentProvider(BaseAgentProvider):
    def __init__(self, transport: AgentTransport) -> None:
        super().__init__(prime_agent_renderer(), transport)


@dataclass(frozen=True, slots=True)
class PrimeAgentTransportConfig:
    cli_path: Path
    endpoint: str
    model: str
    api_key_env: str
    node_binary: str = "node"
    provider_name: str = "autosaddler"

    def __post_init__(self) -> None:
        if not self.cli_path.is_file():
            raise FileNotFoundError(f"Prime Agent CLI is not built: {self.cli_path}")
        for value, label in (
            (self.endpoint, "endpoint"),
            (self.model, "model"),
            (self.api_key_env, "api_key_env"),
            (self.node_binary, "node_binary"),
            (self.provider_name, "provider_name"),
        ):
            if not value:
                raise ValueError(f"Prime Agent transport {label} cannot be empty")


class PrimeAgentTransport:
    """Run one Prime Agent print-mode process and retain its exit transcript."""

    def __init__(self, config: PrimeAgentTransportConfig) -> None:
        self.config = config

    async def run(
        self,
        session: RenderedSession,
        timeout_seconds: float,
    ) -> TransportOutcome:
        require_control_plane_protected(session.workspace)
        if self.config.api_key_env not in os.environ:
            raise RuntimeError(
                f"Prime Agent API key environment variable is unset: "
                f"{self.config.api_key_env}"
            )
        runtime_root = session.workspace / ".autosaddler" / "prime-agent-runtime"
        agent_dir = runtime_root / "agent"
        socket_path = runtime_root / "daemon.sock"
        agent_dir.mkdir(parents=True, exist_ok=True)
        _write_models_json(agent_dir / "models.json", self.config)

        command = [
            self.config.node_binary,
            str(self.config.cli_path),
            "--cwd",
            str(session.workspace),
            "--daemon-socket",
            str(socket_path),
            "--provider",
            self.config.provider_name,
            "--model",
            self.config.model,
            "--system-prompt",
            session.instruction_path.read_text(encoding="utf-8"),
            "--no-session",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--print",
            session.task_prompt,
        ]
        for skill_path in _skill_paths(session.skill_directory):
            command[command.index("--print") : command.index("--print")] = [
                "--skill",
                str(skill_path),
            ]

        environment = os.environ.copy()
        environment.update(
            {
                "PRIME_AGENT_CODING_AGENT_DIR": str(agent_dir),
                "PI_SKIP_VERSION_CHECK": "1",
                "DO_NOT_TRACK": "1",
                "PRIME_AGENT_TELEMETRY": "0",
                MANAGED_WORKSPACE_ENV: "optimizer",
            }
        )
        started = asyncio.get_running_loop().time()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=session.workspace,
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
        finally:
            await _shutdown_private_daemon(
                cli_path=self.config.cli_path,
                node_binary=self.config.node_binary,
                socket_path=socket_path,
                cwd=session.workspace,
                environment=environment,
            )
        duration = asyncio.get_running_loop().time() - started
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        result_record: Mapping[str, JsonValue] = {
            "schema_version": "synth-prime-agent-print/v1",
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "duration_seconds": duration,
        }
        trace_dir = session.trace_dir or runtime_root
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / "prime-agent-print.json").write_text(
            canonical_json(result_record) + "\n",
            encoding="utf-8",
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"Prime Agent print mode exited {process.returncode}: "
                f"{stderr[-2_000:] or stdout[-2_000:]}"
            )
        return TransportOutcome(
            raw_response=stdout.strip(),
            usage=(
                Usage(
                    model=self.config.model,
                    role="optimizer",
                    duration_seconds=duration,
                    usage_incomplete=True,
                ),
            ),
        )


class StubPrimeAgentTransport:
    """Deterministic print-mode substitute used by the Phase 6 smoke."""

    def __init__(self, *, target_path: str, improved_text: str) -> None:
        self.target_path = target_path
        self.improved_text = improved_text
        self._fixture_solutions: dict[str, Mapping[str, str]] = {}

    def prepare_fixture(
        self,
        session_id: str,
        solution: Mapping[str, str],
    ) -> None:
        self._fixture_solutions[session_id] = dict(solution)

    async def run(
        self,
        session: RenderedSession,
        _timeout_seconds: float,
    ) -> TransportOutcome:
        solution = (
            self._fixture_solutions.pop(session.session_id, None)
            if session.session_id is not None
            else None
        )
        if solution is not None:
            return self._run_fixture(session, solution)
        return self._run_optimizer_session(session)

    def _run_fixture(
        self,
        session: RenderedSession,
        solution: Mapping[str, str],
    ) -> TransportOutcome:
        skill_path = session.workspace / self.target_path
        skill_text = (
            skill_path.read_text(encoding="utf-8") if skill_path.is_file() else ""
        )
        repaired = "SYNTH_HARNESS_STRATEGY=repair_seeded_bug" in skill_text
        if repaired:
            for raw_path, content in solution.items():
                destination = session.workspace / raw_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
        transcript = (
            "stub task agent applied deterministic repair"
            if repaired
            else ("stub task agent observed the seeded failure without editing")
        )
        return TransportOutcome(
            raw_response=transcript,
            usage=(Usage(model="stub-prime-agent-v1", role="task_agent"),),
        )

    def _run_optimizer_session(self, session: RenderedSession) -> TransportOutcome:
        context_path = session.workspace / ".autosaddler/session_context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if not isinstance(context, dict):
            raise TypeError("Stub optimizer context must be an object")
        kind = context.get("session_kind")
        if kind == "diagnose_patch":
            protected_roots = require_control_plane_protected(session.workspace)
            if protected_roots != (
                "packages",
                "prime-agent-runtime",
                ".prime/agent/extensions",
            ):
                raise RuntimeError(
                    "stub diagnosis did not receive the protected Synth control plane"
                )
        if kind == "evolve":
            candidate_ids = context.get("candidate_ids")
            if not isinstance(candidate_ids, list) or not candidate_ids:
                raise ValueError("Stub evolution requires candidate IDs")
            response: Mapping[str, JsonValue] = {
                "schema_version": "autosaddler-evolution/v1",
                "parent_ids": [str(candidate_ids[-1])],
                "component_sources": {},
                "rationale": "Continue from the most recently accepted candidate.",
            }
        elif kind == "diagnose_patch":
            destination = session.workspace / self.target_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(self.improved_text, encoding="utf-8")
            response = {
                "schema_version": "autosaddler-diagnosis-patch/v1",
                "changed_paths": [self.target_path],
                "diagnosis": (
                    "The seeded skill observes failures but never repairs them."
                ),
            }
        elif kind == "reflect":
            case_ids = context.get("train_case_ids", [])
            if not isinstance(case_ids, list):
                raise TypeError("Stub reflection train case IDs must be a list")
            response = {
                "schema_version": "autosaddler-reflection/v1",
                "lessons": [
                    {
                        "scope": "steering",
                        "statement": (
                            "Repairing the diagnosed source before rerunning tests "
                            "generalized."
                        ),
                        "evidence_case_ids": case_ids,
                    }
                ],
            }
        else:
            raise ValueError(f"Unknown stub optimizer session kind: {kind}")
        session.output_path.parent.mkdir(parents=True, exist_ok=True)
        session.output_path.write_text(
            canonical_json(response) + "\n", encoding="utf-8"
        )
        return TransportOutcome(
            raw_response=canonical_json(response),
            structured_output=response,
            usage=(
                Usage(input_tokens=5, output_tokens=3, model="stub-prime-agent-v1"),
            ),
        )


def _skill_paths(skill_directory: Path) -> tuple[Path, ...]:
    if not skill_directory.is_dir():
        return ()
    return tuple(
        path.parent
        for path in sorted(skill_directory.glob("*/SKILL.md"))
        if path.is_file()
    )


def _write_models_json(path: Path, config: PrimeAgentTransportConfig) -> None:
    value = {
        "providers": {
            config.provider_name: {
                "baseUrl": config.endpoint,
                "api": "openai-completions",
                "apiKey": config.api_key_env,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [{"id": config.model, "name": config.model}],
            }
        }
    }
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


async def _shutdown_private_daemon(
    *,
    cli_path: Path,
    node_binary: str,
    socket_path: Path,
    cwd: Path,
    environment: Mapping[str, str],
) -> None:
    if not socket_path.exists():
        return
    module_path = cli_path.parent / "cli" / "daemon-launch.js"
    if not module_path.is_file():
        return
    script = (
        "import { shutdownDaemonAndWait } from "
        f"{json.dumps(module_path.as_uri())};"
        "if (!(await shutdownDaemonAndWait(process.argv[1], 10000))) process.exit(1);"
    )
    process = await asyncio.create_subprocess_exec(
        node_binary,
        "--input-type=module",
        "--eval",
        script,
        str(socket_path),
        cwd=cwd,
        env=dict(environment),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=15)
    except TimeoutError:
        process.kill()
        await process.wait()


def find_node(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved is None:
        raise FileNotFoundError(f"Prime Agent node binary is unavailable: {binary}")
    return resolved
