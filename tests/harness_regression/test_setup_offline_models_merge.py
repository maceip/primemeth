from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MERGE_HELPER = ROOT / "scripts" / "merge-local-models.mjs"
NODE = shutil.which("node")


def run_merge(
    agent_dir: Path | str | None,
    *,
    home: Path | None = None,
    template: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    assert NODE is not None
    env = os.environ.copy()
    if agent_dir is None:
        env.pop("PRIME_AGENT_CODING_AGENT_DIR", None)
    else:
        env["PRIME_AGENT_CODING_AGENT_DIR"] = str(agent_dir)
    if home is not None:
        env["HOME"] = str(home)
    command = [NODE, str(MERGE_HELPER)]
    if template is not None:
        command.extend(["--template", str(template)])
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def read_catalog(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def catalog_identity(path: Path) -> tuple[bytes, int, int]:
    path_stat = path.stat()
    return path.read_bytes(), path_stat.st_ino, path_stat.st_mtime_ns


def test_creates_default_home_catalog_with_private_permissions(tmp_path: Path) -> None:
    result = run_merge(None, home=tmp_path)

    destination = tmp_path / ".prime" / "agent" / "models.json"
    assert result.returncode == 0, result.stderr
    assert "models.json: added provider local" in result.stdout
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    local = read_catalog(destination)["providers"]["local"]  # type: ignore[index]
    assert local["baseUrl"] == "http://localhost:8000/v1"  # type: ignore[index]
    assert [model["id"] for model in local["models"]] == [  # type: ignore[index]
        "local-main",
        "local-small",
    ]


def test_adds_missing_fields_and_models_without_replacing_conflicts(
    tmp_path: Path,
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    destination = agent_dir / "models.json"
    destination.write_text(
        """
        {
          // Existing configuration wins conflicts.
          "unknownRoot": {"keep": true},
          "providers": {
            "other": {"baseUrl": "http://localhost:9000/v1",},
            "local": {
              "baseUrl": "http://localhost:11434/v1",
              "api": "openai-completions",
              "apiKey": "EXISTING_LOCAL_KEY",
              "unknownProviderField": true,
              "models": [{"id": "local-main", "name": "Customized Main"},],
            },
          },
        }
        """
    )

    result = run_merge(agent_dir)

    assert result.returncode == 0, result.stderr
    assert "models.json: preserved conflict local.baseUrl" in result.stdout
    assert "models.json: added local.compat" in result.stdout
    assert "models.json: added local model local-small" in result.stdout
    catalog = read_catalog(destination)
    assert catalog["unknownRoot"] == {"keep": True}
    providers = catalog["providers"]  # type: ignore[assignment]
    assert "other" in providers  # type: ignore[operator]
    local = providers["local"]  # type: ignore[index]
    assert local["baseUrl"] == "http://localhost:11434/v1"
    assert local["apiKey"] == "EXISTING_LOCAL_KEY"
    assert local["unknownProviderField"] is True
    assert local["models"][0] == {"id": "local-main", "name": "Customized Main"}
    assert local["models"][1]["id"] == "local-small"


def test_semantic_noop_preserves_bytes_inode_and_mtime(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    destination = agent_dir / "models.json"

    first = run_merge(agent_dir)
    assert first.returncode == 0, first.stderr
    before = catalog_identity(destination)

    second = run_merge(agent_dir)
    after = catalog_identity(destination)

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "models.json: no changes"
    assert after == before


@pytest.mark.parametrize(
    "content,error_fragment",
    [
        ("{", "not valid runtime JSONC"),
        ('{"providers": []}', "providers must be an object"),
        ('{"providers": {"local": []}}', "local must be an object"),
        ('{"providers": {"local": {"models": {}}}}', "models must be an array"),
        (
            '{"providers": {"local": {"models": [{"id": ""}]}}}',
            "id must be a nonempty string",
        ),
    ],
)
def test_rejects_malformed_catalog_without_mutation(
    tmp_path: Path,
    content: str,
    error_fragment: str,
) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    destination = agent_dir / "models.json"
    destination.write_text(content)
    before = destination.read_bytes()

    result = run_merge(agent_dir)

    assert result.returncode != 0
    assert error_fragment in result.stderr
    assert destination.read_bytes() == before
    assert not list(agent_dir.glob(".models.json.synth-*"))


@pytest.mark.parametrize("target_type", ["directory", "symlink"])
def test_rejects_nonregular_destination(tmp_path: Path, target_type: str) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    destination = agent_dir / "models.json"
    if target_type == "directory":
        destination.mkdir()
    else:
        source = tmp_path / "source.json"
        source.write_text('{"providers": {}}')
        destination.symlink_to(source)

    result = run_merge(agent_dir)

    assert result.returncode != 0
    assert "destination must be a regular file" in result.stderr
    assert not list(agent_dir.glob(".models.json.synth-*"))


def test_atomic_update_preserves_mode_and_cleans_temporary_file(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    destination = agent_dir / "models.json"
    destination.write_text('{"providers": {"other": {"baseUrl": "local"}}}\n')
    destination.chmod(0o640)

    result = run_merge(agent_dir)

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert read_catalog(destination)["providers"]["other"] == {  # type: ignore[index]
        "baseUrl": "local"
    }
    assert not list(agent_dir.glob(".models.json.synth-*"))
