from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SOURCE_ROOTS = tuple(
    sorted(
        (
            package / "src"
            for package in (ROOT / "packages").iterdir()
            if (package / "src").is_dir()
        ),
        key=lambda path: path.as_posix(),
    )
)
RUNTIME_ROOTS = (
    ROOT / ".prime" / "agent",
    ROOT / "local_memory",
    ROOT / "middleware",
    ROOT / "optimizer",
    ROOT / "packages" / "coding-agent" / "skills",
    ROOT / "prime-agent-runtime" / "src",
) + PACKAGE_SOURCE_ROOTS
RUNTIME_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
}
CLASSIFIED_REMOTE_PATH_PREFIXES = (
    "packages/ai/src/providers/",
    "packages/ai/src/utils/oauth/",
    "packages/coding-agent/src/core/export-html/vendor/",
    "packages/coding-agent/src/modes/interactive/theme/",
)
CLASSIFIED_REMOTE_PATHS = {
    "packages/agent/src/proxy.ts",
    "packages/ai/src/env-api-keys.ts",
    "packages/ai/src/mcp/catalog.ts",
    "packages/ai/src/models.generated.ts",
    "packages/ai/src/types.ts",
    "packages/coding-agent/skills/linear/src/linear/__init__.py",
    "packages/coding-agent/skills/notion/src/notion/__init__.py",
    "packages/coding-agent/skills/skill-creator/SKILL.md",
    "packages/coding-agent/skills/websearch/SKILL.md",
    "packages/coding-agent/skills/websearch/src/websearch/websearch.py",
    "packages/coding-agent/src/bun/restore-sandbox-env.ts",
    "packages/coding-agent/src/cli/node-version-check.ts",
    "packages/coding-agent/src/config.ts",
    "packages/coding-agent/src/core/extensions/types.ts",
    "packages/coding-agent/src/core/kernel/bootstrap.ts",
    "packages/coding-agent/src/core/model-registry.ts",
    "packages/coding-agent/src/core/prime-inference-auth.ts",
    "packages/coding-agent/src/core/prime-inference-models.ts",
    "packages/coding-agent/src/core/skills.ts",
    "packages/coding-agent/src/core/telemetry.ts",
    "packages/coding-agent/src/migrations.ts",
    "packages/coding-agent/src/modes/interactive/auth-flows.ts",
    "packages/coding-agent/src/modes/interactive/components/daxnuts.ts",
    "packages/coding-agent/src/modes/interactive/components/earendil-announcement.ts",
    "packages/coding-agent/src/modes/interactive/interactive-mode.ts",
    "packages/coding-agent/src/package-manager-cli.ts",
    "packages/coding-agent/src/utils/git.ts",
    "packages/coding-agent/src/utils/shell.ts",
    "packages/coding-agent/src/utils/tools-manager.ts",
    "packages/coding-agent/src/utils/version-check.ts",
    "packages/tui/src/keys.ts",
    "packages/tui/src/stdin-buffer.ts",
    "packages/tui/src/terminal.ts",
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'`<>()]+")


def runtime_files() -> Iterator[Path]:
    for root in RUNTIME_ROOTS:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in RUNTIME_SUFFIXES:
                yield path


def remote_url_hosts() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for source in runtime_files():
        for candidate in URL_PATTERN.findall(source.read_text()):
            host = urlparse(candidate.rstrip(".,;:!?)]}")).hostname
            if host is None or host in LOCAL_HOSTS:
                continue
            relative_path = source.relative_to(ROOT).as_posix()
            result.setdefault(relative_path, set()).add(host)
    return result


def remote_path_is_classified(relative_path: str) -> bool:
    return relative_path in CLASSIFIED_REMOTE_PATHS or relative_path.startswith(
        CLASSIFIED_REMOTE_PATH_PREFIXES
    )


def string_values(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from string_values(child)


def test_nonlocal_urls_exist_only_in_classified_runtime_surfaces() -> None:
    remote_hosts = remote_url_hosts()
    unexpected = {
        relative_path: sorted(hosts)
        for relative_path, hosts in remote_hosts.items()
        if not remote_path_is_classified(relative_path)
    }

    assert remote_hosts
    assert unexpected == {}
    assert "pi.dev" in remote_hosts["packages/coding-agent/src/config.ts"]
    package_cli = (
        ROOT / "packages" / "coding-agent" / "src" / "package-manager-cli.ts"
    ).read_text()
    assert "ssh://git@github.com/user/repo" in URL_PATTERN.findall(package_cli)
    version_check_path = "packages/coding-agent/src/utils/version-check.ts"
    assert (
        "pub-728493de92a943e2a9b2d17b4719f318.r2.dev"
        in remote_hosts[version_check_path]
    )

    version_check = (ROOT / version_check_path).read_text()
    offline_guard = (
        "if (process.env.PI_SKIP_VERSION_CHECK || process.env.PI_OFFLINE) "
        "return undefined;"
    )
    assert version_check.index(offline_guard) < version_check.index("await fetch(")


def test_model_catalog_network_generation_is_not_in_the_build_lifecycle() -> None:
    root_package = json.loads((ROOT / "package.json").read_text())
    ai_package = json.loads((ROOT / "packages" / "ai" / "package.json").read_text())
    generator = ROOT / "packages" / "ai" / "scripts" / "generate-models.ts"

    assert "generate-models" in ai_package["scripts"]
    assert "models.dev" in generator.read_text()
    assert ai_package["scripts"]["build"] == "tsgo -p tsconfig.build.json"
    assert "prebuild" not in ai_package["scripts"]
    assert "postbuild" not in ai_package["scripts"]
    for package in (root_package, ai_package):
        scripts = package["scripts"]
        for lifecycle_name in ("prebuild", "build", "postbuild"):
            command = scripts.get(lifecycle_name, "")
            assert "generate-models" not in command
            assert "models.dev" not in command


def test_shipped_project_defaults_are_local_and_remote_features_are_off() -> None:
    settings = json.loads((ROOT / ".prime" / "agent" / "settings.json").read_text())
    models = json.loads((ROOT / ".prime" / "agent" / "models.json").read_text())
    local = models["providers"]["local"]
    model_ids = {model["id"] for model in local["models"]}

    assert settings["defaultProvider"] == "local"
    assert settings["defaultModel"] in model_ids
    assert settings["daemonSessionSummary"] == {
        "provider": "local",
        "model": "local-small",
    }
    assert settings["telemetry"]["enabled"] is False
    assert settings["bundledSkills"]["websearch"] is False
    assert local["api"] == "openai-completions"
    assert settings["daemonSessionSummary"]["model"] in model_ids

    shipped_config = {"settings": settings, "models": models}
    urls = [value for value in string_values(shipped_config) if "://" in value]
    assert urls
    assert all(urlparse(url).hostname in LOCAL_HOSTS for url in urls)
