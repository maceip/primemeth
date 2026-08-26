"""Model download and provider onboarding for Qwen and other local models."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Variant = Literal["bf16", "mlx", "gguf"]

QWEN_MODEL_VARIANTS: dict[Variant, str] = {
    "bf16": "Qwen/Qwen3.8-Flash-Next",
    "mlx": "mlx-community/Qwen3.8-Flash-Next-4bit",
    "gguf": "Qwen/Qwen3.8-Flash-Next-GGUF",
}


class ModelPullError(Exception):
    """Raised when pulling model weights fails."""


@dataclass(frozen=True, slots=True)
class ModelPullPlan:
    repo_id: str
    variant: str
    target_dir: Path
    estimated_size_bytes: int
    free_disk_bytes: int
    command: list[str]

    def format_plan(self) -> str:
        est_gb = (
            f"{self.estimated_size_bytes / (1024 ** 3):.1f} GB"
            if self.estimated_size_bytes > 0
            else "unknown"
        )
        free_gb = f"{self.free_disk_bytes / (1024 ** 3):.1f} GB"
        cmd_str = " ".join(self.command)
        lines = [
            "[dry-run] Plan for synth model pull:",
            f"  Model ID:         {self.repo_id}",
            f"  Variant:          {self.variant}",
            f"  Target Directory: {self.target_dir}",
            f"  Estimated Size:   {est_gb}",
            f"  Available Disk:   {free_gb}",
            f"  Command:          {cmd_str}",
            "",
            "Next steps after download:",
            "  1. Serve model with MLX LM or vLLM:",
            f"     mlx_lm.server --model {self.target_dir} --port 8080",
            "  2. Wire preset into Prime Agent models catalog:",
            "     node scripts/merge-local-models.mjs",
            "  3. Configure synth.json with Qwen profile:",
            '     { "qwen": { "mode": "thinking", "contextProfile": "native" } }',
        ]
        return "\n".join(lines)


def _resolve_hf_token() -> str | None:
    for env_var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        token = os.environ.get(env_var, "").strip()
        if token:
            return token
    token_file = Path.home() / ".cache" / "huggingface" / "token"
    if token_file.is_file():
        try:
            token = token_file.read_text().strip()
            if token:
                return token
        except OSError:
            pass
    return None


def _resolve_hf_cli() -> str:
    for candidate in (
        shutil.which("hf"),
        "/Users/mac/.local/bin/hf",
        str(Path.home() / ".local" / "bin" / "hf"),
        shutil.which("huggingface-cli"),
    ):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise ModelPullError(
        "Neither 'hf' nor 'huggingface-cli' was found in PATH. "
        "Please ensure Hugging Face CLI is installed."
    )


def resolve_model_repo(model_name: str, variant: Variant) -> str:
    normalized = model_name.strip().lower().replace("_", "-")
    if (
        normalized in ("qwen3.8-flash-next", "qwen3.8", "qwen")
        and variant in QWEN_MODEL_VARIANTS
    ):
        return QWEN_MODEL_VARIANTS[variant]
    if "/" in model_name:
        return model_name.strip()
    if variant in QWEN_MODEL_VARIANTS:
        return QWEN_MODEL_VARIANTS[variant]
    return f"Qwen/{model_name.strip()}"


def check_hf_model_info(repo_id: str) -> tuple[int, bool]:
    token = _resolve_hf_token()
    url = f"https://huggingface.co/api/models/{repo_id}"
    headers = {"User-Agent": "synth-cli/0.1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload: object = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            repo_url = f"https://huggingface.co/{repo_id}"
            raise ModelPullError(
                f"License gate detected for {repo_url} (HTTP {error.code}).\n"
                f"Please visit {repo_url} to accept the license, "
                "ensure `hf auth login` (or `huggingface-cli login`), and try again."
            ) from error
        if error.code == 404:
            raise ModelPullError(
                f"Model repository '{repo_id}' not found on Hugging Face (HTTP 404)."
            ) from error
        raise ModelPullError(
            f"Hugging Face API returned HTTP {error.code}: {error.reason}"
        ) from error
    except urllib.error.URLError as error:
        raise ModelPullError(
            f"Failed to connect to Hugging Face API: {error.reason}"
        ) from error

    if not isinstance(payload, dict):
        return 0, False

    is_gated = bool(payload.get("gated", False))
    used_storage = payload.get("usedStorage")
    safetensors = payload.get("safetensors")
    safetensors_total = (
        safetensors.get("total") if isinstance(safetensors, dict) else None
    )
    siblings = payload.get("siblings")
    siblings_size = (
        sum(
            s.get("size", 0)
            for s in siblings
            if isinstance(s, dict) and isinstance(s.get("size"), int)
        )
        if isinstance(siblings, list)
        else 0
    )

    estimated_size = 0
    if isinstance(safetensors_total, int) and safetensors_total > 0:
        estimated_size = safetensors_total
    elif isinstance(used_storage, int) and used_storage > 0:
        estimated_size = used_storage
    elif siblings_size > 0:
        estimated_size = siblings_size

    return estimated_size, is_gated


def pull_model(
    model_name: str = "qwen3.8-flash-next",
    variant: Variant = "bf16",
    target_dir: Path | None = None,
    dry_run: bool = False,
) -> ModelPullPlan:
    repo_id = resolve_model_repo(model_name, variant)
    repo_name = repo_id.split("/")[-1]

    if target_dir is None:
        resolved_target = Path(f"/Users/mac/models/{repo_name}")
    else:
        resolved_target = target_dir.expanduser().resolve()

    estimated_size, _ = check_hf_model_info(repo_id)

    probe_dir = resolved_target
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        free_disk = shutil.disk_usage(probe_dir).free
    except OSError:
        free_disk = 0

    if estimated_size > 0 and free_disk > 0 and free_disk < estimated_size:
        req_gb = estimated_size / (1024 ** 3)
        avail_gb = free_disk / (1024 ** 3)
        raise ModelPullError(
            f"Insufficient disk space on {probe_dir}: required ~{req_gb:.1f} GB, "
            f"available {avail_gb:.1f} GB."
        )

    hf_cli = _resolve_hf_cli()
    command = [hf_cli, "download", repo_id, "--local-dir", str(resolved_target)]

    plan = ModelPullPlan(
        repo_id=repo_id,
        variant=variant,
        target_dir=resolved_target,
        estimated_size_bytes=estimated_size,
        free_disk_bytes=free_disk,
        command=command,
    )

    if dry_run:
        print(plan.format_plan())
        return plan

    print(f"Pulling model {repo_id} ({variant}) into {resolved_target}...")
    resolved_target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as error:
        raise ModelPullError(
            f"Command failed with exit code {error.returncode}: {' '.join(command)}"
        ) from error

    print("\nDownload complete!")
    print(f"Model directory: {resolved_target}")
    print("\nNext steps:")
    print("1. Serve model:")
    print(f"   mlx_lm.server --model {resolved_target} --port 8080")
    print("2. Wire preset into Prime Agent models catalog:")
    print("   node scripts/merge-local-models.mjs")
    print("3. Configure synth.json with Qwen profile:")
    print('   { "qwen": { "mode": "thinking", "contextProfile": "native" } }')

    return plan
