"""wired into the synth CLI/extension at Phase 7 integration (orphan-check exemption)"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import keyword
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

type CheckStatus = Literal["pass", "fail", "not_run"]

CHECK_PACKAGE_SHAPE = "package_shape"
CHECK_LINT = "lint"
CHECK_TESTS = "tests"
CHECK_STATIC_SAFETY = "static_safety"
CHECK_ORDER = (
    CHECK_PACKAGE_SHAPE,
    CHECK_LINT,
    CHECK_TESTS,
    CHECK_STATIC_SAFETY,
)

NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "boto3",
        "botocore",
        "ftplib",
        "http",
        "httpx",
        "imaplib",
        "paramiko",
        "poplib",
        "requests",
        "smtplib",
        "socket",
        "ssl",
        "telnetlib",
        "urllib",
        "urllib3",
        "websocket",
        "websockets",
        "xmlrpc.client",
    }
)
SELF_MODIFY_NAMES = frozenset(
    {
        "compile_skill",
        "compile_skills",
        "create_skill",
        "create_skills",
        "refine",
        "refine_skill",
        "refine_skills",
    }
)
PATH_MUTATION_METHODS = frozenset(
    {
        "mkdir",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)
OS_SINGLE_PATH_MUTATIONS = frozenset(
    {
        "os.mkdir",
        "os.makedirs",
        "os.remove",
        "os.removedirs",
        "os.rmdir",
        "os.unlink",
    }
)
SHUTIL_DESTINATION_CALLS = frozenset(
    {
        "shutil.copy",
        "shutil.copy2",
        "shutil.copyfile",
        "shutil.copytree",
        "shutil.move",
    }
)
WHEN_NOT_PATTERN = re.compile(
    r"\bwhen[\s-]+not\b|\bdo not use when\b|\bavoid when\b",
    re.IGNORECASE,
)
PYTEST_TIME_PATTERN = re.compile(r"\s+in\s+\d+(?:\.\d+)?s\b")


def _default_repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_temporary_roots() -> tuple[Path, ...]:
    return tuple(
        sorted(
            {Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve()},
            key=lambda path: path.as_posix(),
        )
    )


def _default_uv_cache_dir() -> Path:
    return Path(tempfile.gettempdir()) / "synth-skill-gate-uv-cache"


@dataclass(frozen=True)
class GatePolicy:
    repository_root: Path = field(default_factory=_default_repository_root)
    workspace_roots: tuple[Path, ...] = ()
    temporary_roots: tuple[Path, ...] = field(default_factory=_default_temporary_roots)
    bundled_skills_dir: Path | None = None
    max_file_bytes: int = 64 * 1024
    max_package_bytes: int = 512 * 1024
    uv_binary: str = "uv"
    ruff_version: str = "0.16.4"
    pytest_version: str = "9.1.1"
    tool_timeout_seconds: int = 120
    uv_cache_dir: Path = field(default_factory=_default_uv_cache_dir)

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes must be positive")
        if self.max_package_bytes <= 0:
            raise ValueError("max_package_bytes must be positive")
        if self.tool_timeout_seconds <= 0:
            raise ValueError("tool_timeout_seconds must be positive")

    @property
    def effective_workspace_roots(self) -> tuple[Path, ...]:
        roots = self.workspace_roots or (self.repository_root,)
        return tuple(path.resolve() for path in roots)

    @property
    def effective_bundled_skills_dir(self) -> Path:
        if self.bundled_skills_dir is not None:
            return self.bundled_skills_dir.resolve()
        return (self.repository_root / "packages" / "coding-agent" / "skills").resolve()


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    evidence: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence": list(self.evidence),
            "name": self.name,
            "passed": self.passed,
            "status": self.status,
        }


@dataclass(frozen=True)
class GateResult:
    candidate: str
    passed: bool
    content_hash: str
    checks: tuple[CheckResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate,
            "checks": [check.to_dict() for check in self.checks],
            "content_hash": self.content_hash,
            "passed": self.passed,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def human_summary(self) -> str:
        verdict = "PASS" if self.passed else "REJECT"
        lines = [f"{verdict} {self.candidate} ({self.content_hash})"]
        for check in self.checks:
            lines.append(f"  {check.status.upper():7} {check.name}")
            lines.extend(f"    - {item}" for item in check.evidence)
        return "\n".join(lines)


@dataclass(frozen=True)
class _ShapeContext:
    name: str
    import_name: str
    test_files: tuple[Path, ...]
    frontmatter: Mapping[str, object]


@dataclass(frozen=True, order=True)
class _Finding:
    relative_path: str
    line: int
    category: str
    message: str

    def render(self) -> str:
        location = self.relative_path
        if self.line:
            location = f"{location}:{self.line}"
        return f"{location}: {self.category}: {self.message}"


@dataclass(frozen=True)
class _PathAssessment:
    allowed: bool
    detail: str
    resolved: Path | None = None


def _candidate_entries(candidate_dir: Path) -> tuple[Path, ...]:
    if not candidate_dir.is_dir():
        return ()
    entries: list[Path] = []
    for directory, directory_names, file_names in os.walk(
        candidate_dir, followlinks=False
    ):
        current = Path(directory)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            path = current / name
            if path.is_symlink():
                entries.append(path)
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories
        entries.extend(current / name for name in sorted(file_names))
    return tuple(
        sorted(entries, key=lambda path: path.relative_to(candidate_dir).as_posix())
    )


def _tree_hash(candidate_dir: Path) -> str:
    digest = hashlib.sha256(b"synth-skill-tree-v1\0")
    for path in _candidate_entries(candidate_dir):
        relative = path.relative_to(candidate_dir).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0")
            digest.update(os.readlink(path).encode())
        else:
            content = path.read_bytes()
            digest.update(b"file\0")
            digest.update(str(len(content)).encode())
            digest.update(b"\0")
            digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _parse_scalar(value: str) -> object:
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped.lower() in {"true", "false"}:
        return stripped.lower() == "true"
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        return [str(_parse_scalar(item)).strip() for item in inner.split(",")]
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        try:
            parsed = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            return stripped[1:-1]
        return parsed if isinstance(parsed, str) else stripped[1:-1]
    return stripped


def _parse_frontmatter_text(text: str) -> tuple[dict[str, object], str | None]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "SKILL.md must start with YAML frontmatter"
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration:
        return {}, "SKILL.md frontmatter is not terminated"

    result: dict[str, object] = {}
    index = 1
    while index < end:
        raw = lines[index]
        if not raw.strip() or raw.lstrip().startswith("#"):
            index += 1
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+):\s*(.*)", raw)
        if match is None:
            return {}, f"unsupported frontmatter syntax on line {index + 1}"
        key, raw_value = match.groups()
        if raw_value in {">", ">-", "|", "|-"}:
            block: list[str] = []
            index += 1
            while index < end and (not lines[index] or lines[index][0].isspace()):
                block.append(lines[index].strip())
                index += 1
            separator = "\n" if raw_value.startswith("|") else " "
            result[key] = separator.join(part for part in block if part)
            continue
        if not raw_value:
            items: list[str] = []
            index += 1
            while index < end and lines[index].lstrip().startswith("- "):
                items.append(str(_parse_scalar(lines[index].lstrip()[2:])))
                index += 1
            result[key] = items
            continue
        result[key] = _parse_scalar(raw_value)
        index += 1
    return result, None


def _read_frontmatter(path: Path) -> tuple[dict[str, object], str | None]:
    try:
        return _parse_frontmatter_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as error:
        return {}, f"cannot read SKILL.md: {error.__class__.__name__}"


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _string_list(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _requirement_name(requirement: str) -> str | None:
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", requirement.strip())
    if match is None:
        return None
    return match.group().lower().replace("_", "-")


def _typed_run_error(module_path: Path) -> str | None:
    try:
        tree = ast.parse(
            module_path.read_text(encoding="utf-8"), filename=str(module_path)
        )
    except (OSError, SyntaxError, UnicodeError) as error:
        return f"cannot parse module: {error.__class__.__name__}"
    run_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run"
    ]
    if len(run_nodes) != 1:
        return "module must define exactly one top-level run()"
    run_node = run_nodes[0]
    arguments = (
        list(run_node.args.posonlyargs)
        + list(run_node.args.args)
        + list(run_node.args.kwonlyargs)
    )
    if run_node.args.vararg is not None:
        arguments.append(run_node.args.vararg)
    if run_node.args.kwarg is not None:
        arguments.append(run_node.args.kwarg)
    untyped = [argument.arg for argument in arguments if argument.annotation is None]
    if untyped or run_node.returns is None:
        details = ", ".join(untyped) if untyped else "return value"
        return f"run() has missing type annotations: {details}"
    if ast.get_docstring(run_node) is None:
        return "run() must have a docstring"
    return None


def _package_shape_check(candidate_dir: Path) -> tuple[CheckResult, _ShapeContext]:
    failures: list[str] = []
    evidence: list[str] = []
    directory_name = candidate_dir.name
    skill_path = candidate_dir / "SKILL.md"
    frontmatter: dict[str, object] = {}
    if not candidate_dir.is_dir():
        failures.append("candidate path is not a directory")
    elif not skill_path.is_file():
        failures.append("SKILL.md is missing")
    else:
        frontmatter, error = _read_frontmatter(skill_path)
        if error is not None:
            failures.append(error)

    name_value = frontmatter.get("name")
    name = name_value if isinstance(name_value, str) else directory_name
    if not isinstance(name_value, str) or not name_value:
        failures.append("frontmatter name must be a nonempty string")
    elif name_value != directory_name:
        failures.append(
            f"frontmatter name {name_value!r} does not match "
            f"directory {directory_name!r}"
        )
    else:
        evidence.append(f"frontmatter name {name!r} matches directory")

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        failures.append("frontmatter description must be a nonempty string")
    elif (
        "when" not in description.lower()
        or WHEN_NOT_PATTERN.search(description) is None
    ):
        failures.append(
            "description must state both when and when-NOT to use the skill"
        )
    else:
        evidence.append("description states when and when-NOT to use the skill")

    import_name = name.replace("-", "_")
    if not import_name.isidentifier() or keyword.iskeyword(import_name):
        failures.append(
            f"derived import name {import_name!r} is not a Python identifier"
        )

    pyproject_path = candidate_dir / "pyproject.toml"
    pyproject: Mapping[str, object] | None = None
    if not pyproject_path.is_file():
        failures.append("pyproject.toml is missing")
    else:
        try:
            pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeError) as error:
            failures.append(f"pyproject.toml is invalid: {error.__class__.__name__}")

    if pyproject is not None:
        pyproject_failure_count = len(failures)
        project = _mapping(pyproject.get("project"))
        build_system = _mapping(pyproject.get("build-system"))
        tool = _mapping(pyproject.get("tool"))
        hatch = _mapping(tool.get("hatch")) if tool is not None else None
        build = _mapping(hatch.get("build")) if hatch is not None else None
        targets = _mapping(build.get("targets")) if build is not None else None
        wheel = _mapping(targets.get("wheel")) if targets is not None else None
        expected_package = f"src/{import_name}"
        if project is None or project.get("name") != name:
            failures.append("pyproject [project].name must match the skill name")
        if project is None or not isinstance(project.get("requires-python"), str):
            failures.append("pyproject [project].requires-python must be declared")
        dependencies = _string_list(project.get("dependencies")) if project else None
        if dependencies is None:
            failures.append("pyproject [project].dependencies must be a string list")
        elif any(
            _requirement_name(dependency) == "prime-agent-runtime"
            for dependency in dependencies
        ):
            failures.append("prime-agent-runtime must not be declared as a dependency")
        build_requirements = (
            _string_list(build_system.get("requires")) if build_system else None
        )
        if (
            build_system is None
            or build_system.get("build-backend") != "hatchling.build"
            or build_requirements is None
            or not any(
                _requirement_name(item) == "hatchling"
                for item in build_requirements
            )
        ):
            failures.append("pyproject must use the hatchling build backend")
        wheel_packages = _string_list(wheel.get("packages")) if wheel else None
        if wheel_packages is None or expected_package not in wheel_packages:
            failures.append(f"wheel packages must include {expected_package!r}")
        if len(failures) == pyproject_failure_count:
            evidence.append(f"pyproject metadata maps {name!r} to {expected_package!r}")

    module_path = candidate_dir / "src" / import_name / "__init__.py"
    if not module_path.is_file():
        failures.append(f"src/{import_name}/__init__.py is missing")
    else:
        run_error = _typed_run_error(module_path)
        if run_error is not None:
            failures.append(f"src/{import_name}/__init__.py: {run_error}")
        else:
            evidence.append(f"src/{import_name}/__init__.py defines typed run()")

    expected_test = candidate_dir / "tests" / f"test_{import_name}.py"
    test_files = tuple(
        sorted(
            (candidate_dir / "tests").glob("test_*.py"),
            key=lambda path: path.name,
        )
    )
    if not expected_test.is_file():
        failures.append(f"tests/test_{import_name}.py is missing")
    else:
        evidence.append(f"tests/test_{import_name}.py exists")

    status: CheckStatus = "fail" if failures else "pass"
    return (
        CheckResult(
            name=CHECK_PACKAGE_SHAPE,
            status=status,
            evidence=tuple(failures or evidence),
        ),
        _ShapeContext(
            name=name,
            import_name=import_name,
            test_files=test_files,
            frontmatter=frontmatter,
        ),
    )


def _network_declaration(
    frontmatter: Mapping[str, object],
) -> tuple[bool, frozenset[str], str | None]:
    value = frontmatter.get("network-imports", frontmatter.get("network"))
    if value is None or value is False:
        return False, frozenset(), None
    if value is True:
        return True, frozenset(), None
    if isinstance(value, str):
        names = (value,)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        names = tuple(value)
    else:
        return (
            False,
            frozenset(),
            "network declaration must be true or a module-name list",
        )
    normalized = frozenset(name.strip() for name in names if name.strip())
    if not normalized:
        return False, frozenset(), "network declaration module list must not be empty"
    return False, normalized, None


def _module_is_network_capable(module: str) -> bool:
    return any(
        module == network_module or module.startswith(f"{network_module}.")
        for network_module in NETWORK_MODULES
    )


def _module_is_declared(module: str, declared: frozenset[str]) -> bool:
    return any(
        module == item or module.startswith(f"{item}.") or item.startswith(f"{module}.")
        for item in declared
    )


def _within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


class _SafetyVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        candidate_dir: Path,
        relative_path: str,
        policy: GatePolicy,
        allow_all_network: bool,
        declared_network: frozenset[str],
    ) -> None:
        self.candidate_dir = candidate_dir
        self.relative_path = relative_path
        self.allowed_roots = policy.effective_workspace_roots + tuple(
            path.resolve() for path in policy.temporary_roots
        )
        self.allow_all_network = allow_all_network
        self.declared_network = declared_network
        self.findings: list[_Finding] = []
        self.aliases: list[dict[str, str]] = [{}]
        self.path_bindings: list[dict[str, _PathAssessment]] = [{}]

    def _add(self, category: str, node: ast.AST, message: str) -> None:
        self.findings.append(
            _Finding(
                relative_path=self.relative_path,
                line=getattr(node, "lineno", 0),
                category=category,
                message=message,
            )
        )

    def _alias(self, name: str) -> str | None:
        for scope in reversed(self.aliases):
            if name in scope:
                return scope[name]
        return None

    def _qualified_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self._alias(node.id) or node.id
        if isinstance(node, ast.Attribute):
            base = self._qualified_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    def _path_binding(self, name: str) -> _PathAssessment | None:
        for scope in reversed(self.path_bindings):
            if name in scope:
                return scope[name]
        return None

    def _literal_assessment(self, raw: str) -> _PathAssessment:
        path = Path(raw)
        resolved = (
            path.resolve()
            if path.is_absolute()
            else (self.candidate_dir / path).resolve()
        )
        if _within(resolved, self.allowed_roots):
            return _PathAssessment(True, raw, resolved)
        return _PathAssessment(False, raw, resolved)

    def _assess_path(self, node: ast.AST) -> _PathAssessment:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return self._literal_assessment(node.value)
        if isinstance(node, ast.Name):
            return self._path_binding(node.id) or _PathAssessment(
                False, f"unresolved name {node.id!r}"
            )
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left = self._assess_path(node.left)
            if not left.allowed or left.resolved is None:
                return left
            if not isinstance(node.right, ast.Constant) or not isinstance(
                node.right.value, str
            ):
                return _PathAssessment(False, "dynamic path component")
            combined = (left.resolved / node.right.value).resolve()
            return _PathAssessment(
                _within(combined, self.allowed_roots),
                combined.as_posix(),
                combined,
            )
        if isinstance(node, ast.Call):
            qualified = self._qualified_name(node.func)
            if qualified in {"Path", "pathlib.Path"} and node.args:
                return self._assess_path(node.args[0])
            if qualified in {"Path.cwd", "pathlib.Path.cwd"}:
                return _PathAssessment(True, "workspace cwd", self.candidate_dir)
            if qualified in {"tempfile.gettempdir", "tempfile.mkdtemp"}:
                temporary_root = Path(tempfile.gettempdir()).resolve()
                return _PathAssessment(True, "temporary directory", temporary_root)
            if qualified in {"os.path.join", "posixpath.join"} and node.args:
                base = self._assess_path(node.args[0])
                if not base.allowed or base.resolved is None:
                    return base
                combined = base.resolved
                for part in node.args[1:]:
                    if not isinstance(part, ast.Constant) or not isinstance(
                        part.value, str
                    ):
                        return _PathAssessment(False, "dynamic path component")
                    combined = combined / part.value
                combined = combined.resolve()
                return _PathAssessment(
                    _within(combined, self.allowed_roots),
                    combined.as_posix(),
                    combined,
                )
            if (
                qualified
                and qualified.rsplit(".", maxsplit=1)[-1]
                in {
                    "absolute",
                    "resolve",
                    "with_name",
                    "with_stem",
                    "with_suffix",
                }
                and isinstance(node.func, ast.Attribute)
            ):
                return self._assess_path(node.func.value)
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self._assess_path(node.value)
            if base.resolved is not None:
                parent = base.resolved.parent
                return _PathAssessment(
                    _within(parent, self.allowed_roots),
                    parent.as_posix(),
                    parent,
                )
        return _PathAssessment(False, "dynamic or unresolved path")

    def _check_write_target(
        self, node: ast.AST, target: ast.AST, operation: str
    ) -> None:
        assessment = self._assess_path(target)
        if not assessment.allowed:
            self._add(
                "filesystem",
                node,
                f"{operation} target is outside workspace/tmp roots or unresolved: "
                f"{assessment.detail}",
            )

    @staticmethod
    def _literal_string(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    def _open_writes(self, node: ast.Call) -> bool:
        mode_node: ast.AST | None = None
        if len(node.args) >= 2:
            mode_node = node.args[1]
        for keyword_node in node.keywords:
            if keyword_node.arg == "mode":
                mode_node = keyword_node.value
        if mode_node is None:
            return False
        mode = self._literal_string(mode_node)
        return mode is None or any(character in mode for character in "wax+")

    def _check_network_module(self, node: ast.AST, module: str) -> None:
        if (
            _module_is_network_capable(module)
            and not self.allow_all_network
            and not _module_is_declared(module, self.declared_network)
        ):
            self._add(
                "network",
                node,
                f"network-capable import {module!r} is not declared in SKILL.md",
            )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_network_module(node, alias.name)
            local_name = alias.asname or alias.name.split(".", maxsplit=1)[0]
            self.aliases[-1][local_name] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module is None:
            return
        self._check_network_module(node, node.module)
        for alias in node.names:
            local_name = alias.asname or alias.name
            self.aliases[-1][local_name] = f"{node.module}.{alias.name}"

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        assessment = self._assess_path(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.path_bindings[-1][target.id] = assessment

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self.path_bindings[-1][node.target.id] = self._assess_path(node.value)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        self.aliases.append({})
        self.path_bindings.append({})
        temporary_root = Path(tempfile.gettempdir()).resolve()
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ):
            if argument.arg in {"tmp_path", "tmpdir"}:
                self.path_bindings[-1][argument.arg] = _PathAssessment(
                    True,
                    "pytest temporary directory",
                    temporary_root,
                )
        for statement in node.body:
            self.visit(statement)
        self.path_bindings.pop()
        self.aliases.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        qualified = self._qualified_name(node.func)
        leaf = qualified.rsplit(".", maxsplit=1)[-1] if qualified else ""

        if qualified in {"__import__", "importlib.import_module"} and node.args:
            module = self._literal_string(node.args[0])
            if module is not None:
                self._check_network_module(node, module)

        if leaf in SELF_MODIFY_NAMES or (
            "skill" in leaf and ("refin" in leaf or "compil" in leaf)
        ):
            self._add(
                "self_modification",
                node,
                f"invocation of {qualified or leaf!r} is forbidden",
            )
        elif qualified in {
            "rlm.run",
            "subprocess.call",
            "subprocess.run",
            "subprocess.Popen",
        }:
            strings = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            ]
            invocation = " ".join(strings).lower()
            if "skill" in invocation and (
                "refin" in invocation or "compil" in invocation
            ):
                self._add(
                    "self_modification",
                    node,
                    f"{qualified} invokes skill compilation/refinement",
                )

        if qualified == "open" and node.args and self._open_writes(node):
            self._check_write_target(node, node.args[0], "open")
        elif (
            leaf == "open"
            and isinstance(node.func, ast.Attribute)
            and self._open_writes(node)
        ):
            self._check_write_target(node, node.func.value, "Path.open")
        elif leaf in PATH_MUTATION_METHODS and isinstance(node.func, ast.Attribute):
            self._check_write_target(node, node.func.value, leaf)
        elif leaf in {"rename", "replace"} and isinstance(node.func, ast.Attribute):
            self._check_write_target(node, node.func.value, leaf)
            if node.args:
                self._check_write_target(node, node.args[0], f"{leaf} destination")
        elif qualified in OS_SINGLE_PATH_MUTATIONS and node.args:
            self._check_write_target(node, node.args[0], qualified)
        elif qualified in {"os.rename", "os.replace"} and len(node.args) >= 2:
            self._check_write_target(node, node.args[0], qualified)
            self._check_write_target(node, node.args[1], f"{qualified} destination")
        elif qualified in SHUTIL_DESTINATION_CALLS and len(node.args) >= 2:
            self._check_write_target(node, node.args[1], qualified)
        elif qualified == "os.open" and len(node.args) >= 2:
            flag_names = {
                child.attr if isinstance(child, ast.Attribute) else child.id
                for child in ast.walk(node.args[1])
                if isinstance(child, (ast.Attribute, ast.Name))
            }
            if flag_names & {"O_APPEND", "O_CREAT", "O_RDWR", "O_TRUNC", "O_WRONLY"}:
                self._check_write_target(node, node.args[0], "os.open")

        self.generic_visit(node)


def _static_safety_check(
    candidate_dir: Path, policy: GatePolicy, context: _ShapeContext
) -> CheckResult:
    findings: list[_Finding] = []
    evidence: list[str] = []
    entries = _candidate_entries(candidate_dir)
    package_size = 0
    regular_files = 0
    for path in entries:
        relative = path.relative_to(candidate_dir).as_posix()
        if path.is_symlink():
            findings.append(
                _Finding(relative, 0, "filesystem", "symbolic links are forbidden")
            )
            continue
        size = path.stat().st_size
        package_size += size
        regular_files += 1
        if size > policy.max_file_bytes:
            findings.append(
                _Finding(
                    relative,
                    0,
                    "size",
                    f"{size} bytes exceeds {policy.max_file_bytes}-byte file cap",
                )
            )
    if package_size > policy.max_package_bytes:
        findings.append(
            _Finding(
                ".",
                0,
                "size",
                f"{package_size} bytes exceeds "
                f"{policy.max_package_bytes}-byte package cap",
            )
        )

    bundled_dir = policy.effective_bundled_skills_dir
    if not bundled_dir.is_dir():
        findings.append(
            _Finding(
                ".",
                0,
                "shadowing",
                f"bundled skill directory is unavailable: {bundled_dir.as_posix()}",
            )
        )
        bundled_names: tuple[str, ...] = ()
    else:
        bundled_names = tuple(
            sorted(path.name for path in bundled_dir.iterdir() if path.is_dir())
        )
        normalized_bundled = {name.replace("-", "_") for name in bundled_names}
        candidate_names = {candidate_dir.name, context.name}
        if any(
            name in bundled_names or name.replace("-", "_") in normalized_bundled
            for name in candidate_names
        ):
            findings.append(
                _Finding(
                    "SKILL.md",
                    0,
                    "shadowing",
                    f"skill name {context.name!r} shadows a bundled skill",
                )
            )

    allow_all_network, declared_network, declaration_error = _network_declaration(
        context.frontmatter
    )
    if declaration_error is not None:
        findings.append(_Finding("SKILL.md", 0, "network", declaration_error))

    python_files = tuple(
        path for path in entries if not path.is_symlink() and path.suffix == ".py"
    )
    for path in python_files:
        relative = path.relative_to(candidate_dir).as_posix()
        if path.stat().st_size > policy.max_file_bytes:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError, UnicodeError) as error:
            findings.append(
                _Finding(
                    relative,
                    getattr(error, "lineno", 0) or 0,
                    "syntax",
                    f"cannot statically inspect file: {error.__class__.__name__}",
                )
            )
            continue
        visitor = _SafetyVisitor(
            candidate_dir=candidate_dir,
            relative_path=relative,
            policy=policy,
            allow_all_network=allow_all_network,
            declared_network=declared_network,
        )
        visitor.visit(tree)
        findings.extend(visitor.findings)

    if findings:
        return CheckResult(
            name=CHECK_STATIC_SAFETY,
            status="fail",
            evidence=tuple(finding.render() for finding in sorted(set(findings))),
        )

    network_evidence = (
        "all network modules"
        if allow_all_network
        else (", ".join(sorted(declared_network)) or "none")
    )
    evidence.extend(
        (
            f"size {package_size} bytes across {regular_files} files; caps "
            f"{policy.max_file_bytes}/{policy.max_package_bytes} bytes",
            f"network declarations: {network_evidence}; imports are compliant",
            "filesystem mutations are confined to workspace/tmp paths",
            "no skill compilation or refinement invocation found",
            f"name is clear of {len(bundled_names)} bundled skills",
        )
    )
    return CheckResult(CHECK_STATIC_SAFETY, "pass", tuple(evidence))


def _tool_environment(policy: GatePolicy) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "UV_CACHE_DIR": policy.uv_cache_dir.resolve().as_posix(),
            "UV_NO_PROGRESS": "1",
        }
    )
    return environment


def _run_uv_tool(
    candidate_dir: Path,
    policy: GatePolicy,
    arguments: Sequence[str],
) -> tuple[int | None, str]:
    uv_path = shutil.which(policy.uv_binary)
    if uv_path is None:
        return None, f"uv executable {policy.uv_binary!r} was not found"
    command = [
        uv_path,
        "run",
        "--quiet",
        "--no-progress",
        "--color",
        "never",
        "--no-project",
        "--no-config",
        "--isolated",
        "--python",
        "3.12",
        "--managed-python",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=candidate_dir,
            env=_tool_environment(policy),
            capture_output=True,
            text=True,
            check=False,
            timeout=policy.tool_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return None, f"uv tool command timed out after {policy.tool_timeout_seconds}s"
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    output = output.replace(candidate_dir.as_posix(), ".")
    output = PYTEST_TIME_PATTERN.sub("", output)
    return result.returncode, output.strip()


def _lint_check(candidate_dir: Path, policy: GatePolicy) -> CheckResult:
    if not candidate_dir.is_dir():
        return CheckResult(
            CHECK_LINT, "not_run", ("candidate directory is unavailable",)
        )
    return_code, output = _run_uv_tool(
        candidate_dir,
        policy,
        (
            "--with",
            f"ruff=={policy.ruff_version}",
            "ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--output-format",
            "concise",
            ".",
        ),
    )
    command_evidence = (
        f"uv run --no-project --isolated --with ruff=={policy.ruff_version} ruff check"
    )
    if return_code == 0:
        detail = output or "All checks passed"
        return CheckResult(CHECK_LINT, "pass", (command_evidence, detail))
    detail = output or "ruff could not be executed"
    return CheckResult(CHECK_LINT, "fail", (command_evidence, detail))


def _assertion_scan(test_files: Sequence[Path]) -> tuple[int, tuple[str, ...]]:
    failures: list[str] = []
    for path in test_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(
                f"{path.name}: cannot inspect assertions: {error.__class__.__name__}"
            )
            continue
        has_assert = any(isinstance(node, ast.Assert) for node in ast.walk(tree))
        has_raises = any(
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "raises")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "raises")
            )
            for node in ast.walk(tree)
        )
        if not has_assert and not has_raises:
            failures.append(f"{path.name}: assertion-free test file is forbidden")
    return len(test_files), tuple(failures)


def _pytest_failure_evidence(output: str, return_code: int) -> str:
    node_ids = sorted(
        {
            match.group(1)
            for match in re.finditer(
                r"^(?:FAILED|ERROR)\s+([^\s]+)", output, re.MULTILINE
            )
        }
    )
    if node_ids:
        return f"pytest exited {return_code}; failing nodes: {', '.join(node_ids)}"
    return f"pytest or candidate installation exited {return_code}"


def _tests_check(
    candidate_dir: Path,
    policy: GatePolicy,
    context: _ShapeContext,
    shape: CheckResult,
    safety: CheckResult,
) -> CheckResult:
    test_count, assertion_failures = _assertion_scan(context.test_files)
    if assertion_failures:
        return CheckResult(CHECK_TESTS, "fail", assertion_failures)
    assertion_evidence = f"{test_count} test file(s) contain assert/raises"
    if not shape.passed:
        return CheckResult(
            CHECK_TESTS,
            "not_run",
            (assertion_evidence, "package-shape preflight failed"),
        )
    if not safety.passed:
        return CheckResult(
            CHECK_TESTS,
            "not_run",
            (
                assertion_evidence,
                "static-safety preflight failed; candidate was not executed",
            ),
        )
    return_code, output = _run_uv_tool(
        candidate_dir,
        policy,
        (
            "--with",
            f"pytest=={policy.pytest_version}",
            "--with",
            ".",
            "pytest",
            "-q",
            "-c",
            "pyproject.toml",
            "-p",
            "no:cacheprovider",
            "--disable-warnings",
            "--tb=short",
            *(
                path.relative_to(candidate_dir).as_posix()
                for path in context.test_files
            ),
        ),
    )
    command_evidence = (
        f"uv run --no-project --isolated --with pytest=={policy.pytest_version} "
        "--with . pytest"
    )
    if return_code == 0:
        matches = re.findall(r"\b(\d+) passed\b", output)
        summary = f"{matches[-1]} passed" if matches else "pytest exited 0"
        return CheckResult(
            CHECK_TESTS,
            "pass",
            (assertion_evidence, command_evidence, summary),
        )
    return CheckResult(
        CHECK_TESTS,
        "fail",
        (
            assertion_evidence,
            command_evidence,
            _pytest_failure_evidence(output, return_code)
            if return_code is not None
            else output,
        ),
    )


def evaluate(candidate_dir: str | Path, policy: GatePolicy | None = None) -> GateResult:
    active_policy = policy or GatePolicy()
    candidate = Path(candidate_dir).resolve()
    content_hash = _tree_hash(candidate)
    shape, context = _package_shape_check(candidate)
    safety = _static_safety_check(candidate, active_policy, context)
    lint = _lint_check(candidate, active_policy)
    tests = _tests_check(candidate, active_policy, context, shape, safety)
    checks = (shape, lint, tests, safety)
    if tuple(check.name for check in checks) != CHECK_ORDER:
        raise AssertionError("internal check order is invalid")
    return GateResult(
        candidate=candidate.name,
        passed=all(check.passed for check in checks),
        content_hash=content_hash,
        checks=checks,
    )


def verdict_table(results: Sequence[GateResult]) -> str:
    headers = (
        "candidate",
        "verdict",
        "shape",
        "lint",
        "tests",
        "safety",
        "rejected_by",
    )
    rows: list[tuple[str, ...]] = []
    for result in results:
        by_name = {check.name: check for check in result.checks}
        failed = [check.name for check in result.checks if check.status == "fail"]
        if not failed:
            failed = [
                check.name for check in result.checks if check.status == "not_run"
            ]
        rows.append(
            (
                result.candidate,
                "PASS" if result.passed else "REJECT",
                by_name[CHECK_PACKAGE_SHAPE].status.upper(),
                by_name[CHECK_LINT].status.upper(),
                by_name[CHECK_TESTS].status.upper(),
                by_name[CHECK_STATIC_SAFETY].status.upper(),
                ",".join(failed) or "-",
            )
        )
    widths = (
        [
            max(len(headers[index]), *(len(row[index]) for row in rows))
            for index in range(len(headers))
        ]
        if rows
        else [len(header) for header in headers]
    )

    def render(row: Sequence[str]) -> str:
        return "  ".join(
            value.ljust(width) for value, width in zip(row, widths, strict=True)
        )

    separator = "  ".join("-" * width for width in widths)
    return "\n".join((render(headers), separator, *(render(row) for row in rows)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate generated skill packages")
    parser.add_argument("candidates", nargs="+", type=Path)
    parser.add_argument(
        "--format",
        choices=("json", "summary", "table"),
        default="summary",
        dest="output_format",
    )
    parser.add_argument(
        "--allow-rejections",
        action="store_true",
        help="return success after rendering rejected candidates",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    candidates = sorted(arguments.candidates, key=lambda path: path.name)
    results = tuple(evaluate(path) for path in candidates)
    if arguments.output_format == "json":
        print(
            json.dumps(
                [result.to_dict() for result in results],
                indent=2,
                sort_keys=True,
            )
        )
    elif arguments.output_format == "table":
        print(verdict_table(results))
    else:
        print("\n\n".join(result.human_summary() for result in results))
    if arguments.allow_rejections or all(result.passed for result in results):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
