"""Model download and provider onboarding for Qwen and other local models."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Variant = Literal["bf16", "mlx", "gguf"]
DEFAULT_QWEN_GGUF_QUANT = "UD-Q4_K_XL"

QWEN_MODEL_VARIANTS: dict[Variant, str] = {
    "bf16": "Qwen/Qwen3.8-Flash-Next",
    "mlx": "Qwen/Qwen3.8-Flash-Next",
    "gguf": "unsloth/Qwen3.8-Flash-Next-GGUF",
}


class ModelPullError(Exception):
    """Raised when pulling model weights fails."""


@dataclass(frozen=True, slots=True)
class ModelPullPlan:
    repo_id: str
    variant: Variant
    target_dir: Path
    estimated_size_bytes: int
    free_disk_bytes: int
    command: list[str]
    selected_quant: str | None = None
    selected_files: tuple[str, ...] = ()

    def gguf_entrypoint(self) -> Path:
        if self.variant != "gguf":
            raise ModelPullError("GGUF entrypoint requested for non-GGUF weights")
        gguf_files = sorted(
            name for name in self.selected_files if name.casefold().endswith(".gguf")
        )
        if not gguf_files:
            raise ModelPullError("selected GGUF quantization contains no GGUF weights")
        if len(gguf_files) == 1:
            return self.target_dir / gguf_files[0]
        matches = [_GGUF_SHARD_RE.fullmatch(name) for name in gguf_files]
        if any(match is None for match in matches):
            raise ModelPullError(
                "selected GGUF quantization is ambiguous; expected one file or one "
                "complete split-shard set"
            )
        matched = [match for match in matches if match is not None]
        prefixes = {match.group("prefix") for match in matched}
        totals = {int(match.group("total")) for match in matched}
        indexes = {int(match.group("index")) for match in matched}
        if len(prefixes) != 1 or len(totals) != 1:
            raise ModelPullError(
                "selected GGUF quantization contains multiple shard sets"
            )
        total = next(iter(totals))
        if indexes != set(range(1, total + 1)):
            raise ModelPullError(
                f"selected GGUF quantization is incomplete: found {len(indexes)} of "
                f"{total} shards"
            )
        first = next(
            name
            for name, match in zip(gguf_files, matched, strict=True)
            if int(match.group("index")) == 1
        )
        return self.target_dir / first

    def mlx_output(self) -> Path:
        return self.target_dir.with_name(f"{self.target_dir.name}-MLX-mixed46")

    def serve_command(self) -> list[str]:
        if self.variant == "mlx":
            model = self.mlx_output()
        elif self.variant == "gguf":
            model = self.gguf_entrypoint()
        else:
            model = self.target_dir
        base = ["synth", "serve", "start", "--model", str(model)]
        if self.variant == "bf16":
            return [*base, "--engine", "transformers", "--quantize", "bnb-4bit"]
        if self.variant == "mlx":
            return [*base, "--engine", "mlx"]
        return [*base, "--engine", "llama.cpp"]

    def next_steps(self) -> list[str]:
        if self.variant == "mlx":
            conversion = [
                "synth",
                "model",
                "quantize",
                "--input",
                str(self.target_dir),
                "--output",
                str(self.mlx_output()),
            ]
            return [
                "Next steps after download:",
                "  1. Convert the BF16 source with Synth's mixed46 recipe:",
                f"     {shlex.join(conversion)}",
                "  2. Serve the converted MLX weights:",
                f"     {shlex.join(self.serve_command())}",
                "  3. The tracked Prime Agent model presets are already available in:",
                "     .prime/agent/models.json",
                "  4. Configure the optional Qwen runtime profile in synth.json:",
                '     { "qwen": { "mode": "thinking", "contextProfile": "native" } }',
            ]
        return [
            "Next steps after download:",
            f"  1. Serve the {self.variant} weights:",
            f"     {shlex.join(self.serve_command())}",
            "  2. The tracked Prime Agent model presets are already available in:",
            "     .prime/agent/models.json",
            "  3. Configure the optional Qwen runtime profile in synth.json:",
            '     { "qwen": { "mode": "thinking", "contextProfile": "native" } }',
        ]

    def format_plan(self) -> str:
        est_gb = (
            f"{self.estimated_size_bytes / (1024**3):.1f} GB"
            if self.estimated_size_bytes > 0
            else "unknown"
        )
        free_gb = f"{self.free_disk_bytes / (1024**3):.1f} GB"
        cmd_str = shlex.join(self.command)
        lines = [
            "[dry-run] Plan for synth model pull:",
            f"  Model ID:         {self.repo_id}",
            f"  Variant:          {self.variant}",
            *(
                [f"  GGUF Quant:       {self.selected_quant}"]
                if self.selected_quant is not None
                else []
            ),
            f"  Target Directory: {self.target_dir}",
            *(
                [f"  GGUF Entrypoint:  {self.gguf_entrypoint()}"]
                if self.variant == "gguf"
                else []
            ),
            f"  Estimated Size:   {est_gb}",
            f"  Available Disk:   {free_gb}",
            f"  Command:          {cmd_str}",
            "",
            *self.next_steps(),
        ]
        return "\n".join(lines)


def _resolve_hf_cli() -> str:
    for candidate in (
        shutil.which("hf"),
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
    raise ModelPullError(
        f"unknown model shorthand: {model_name!r}; pass an explicit owner/repository ID"
    )


def _size_bytes(value: object) -> int:
    if not isinstance(value, str):
        return 0
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGTPE]?)", value.strip())
    if match is None:
        return 0
    units = {"": 1, "K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
    units.update({"T": 10**12, "P": 10**15, "E": 10**18})
    return round(float(match.group(1)) * units[match.group(2)])


_GGUF_QUANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_GGUF_SHARD_RE = re.compile(
    r"(?P<prefix>.+)-(?P<index>\d{5})-of-(?P<total>\d{5})\.gguf",
    re.IGNORECASE,
)


def check_hf_download_plan(
    hf_cli: str, repo_id: str, include_pattern: str | None = None
) -> tuple[int, tuple[str, ...]]:
    command = [hf_cli, "download", repo_id]
    if include_pattern is not None:
        command.extend(["--include", include_pattern])
    command.extend(["--dry-run", "--format", "json"])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ModelPullError(f"Failed to inspect model download: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        lowered = detail.casefold()
        if any(
            marker in lowered
            for marker in ("gatedrepo", "gated repo", "access to model", "401", "403")
        ):
            raise ModelPullError(
                f"License gate detected for {repo_id}. Accept the model license "
                "on Hugging Face, run `hf auth login`, and try again."
            )
        if "404" in lowered or "not found" in lowered:
            raise ModelPullError(
                f"Model repository '{repo_id}' was not found on Hugging Face."
            )
        raise ModelPullError(
            f"Could not inspect model repository '{repo_id}': {detail}"
        )
    try:
        payload: object = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ModelPullError(
            "hf download returned an invalid dry-run receipt"
        ) from error
    if not isinstance(payload, list):
        raise ModelPullError("hf download returned an invalid dry-run file list")
    files = tuple(
        name
        for item in payload
        if isinstance(item, dict)
        and isinstance((name := item.get("file")), str)
        and name
    )
    if not files:
        scope = f" matching {include_pattern!r}" if include_pattern else ""
        raise ModelPullError(f"model repository '{repo_id}' has no files{scope}")
    return (
        sum(
            _size_bytes(item.get("size")) for item in payload if isinstance(item, dict)
        ),
        files,
    )


def pull_model(
    model_name: str = "qwen3.8-flash-next",
    variant: Variant = "bf16",
    target_dir: Path | None = None,
    dry_run: bool = False,
    gguf_quant: str | None = None,
) -> ModelPullPlan:
    repo_id = resolve_model_repo(model_name, variant)
    repo_name = repo_id.split("/")[-1]

    if target_dir is None:
        resolved_target = (Path.home() / "models" / repo_name).resolve()
    else:
        resolved_target = target_dir.expanduser().resolve()

    if variant != "gguf" and gguf_quant is not None:
        raise ModelPullError("--gguf-quant is only valid with --variant gguf")
    selected_quant = (
        gguf_quant or DEFAULT_QWEN_GGUF_QUANT if variant == "gguf" else None
    )
    if selected_quant is not None and _GGUF_QUANT_RE.fullmatch(selected_quant) is None:
        raise ModelPullError(
            "--gguf-quant must contain only letters, digits, dot, dash, or underscore"
        )
    include_pattern = f"{selected_quant}/*.gguf" if selected_quant is not None else None

    hf_cli = _resolve_hf_cli()
    estimated_size, selected_files = check_hf_download_plan(
        hf_cli, repo_id, include_pattern
    )

    probe_dir = resolved_target
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        free_disk = shutil.disk_usage(probe_dir).free
    except OSError:
        free_disk = 0

    if estimated_size > 0 and free_disk > 0 and free_disk < estimated_size:
        req_gb = estimated_size / (1024**3)
        avail_gb = free_disk / (1024**3)
        raise ModelPullError(
            f"Insufficient disk space on {probe_dir}: required ~{req_gb:.1f} GB, "
            f"available {avail_gb:.1f} GB."
        )

    command = [hf_cli, "download", repo_id]
    if include_pattern is not None:
        command.extend(["--include", include_pattern])
    command.extend(["--local-dir", str(resolved_target)])

    plan = ModelPullPlan(
        repo_id=repo_id,
        variant=variant,
        target_dir=resolved_target,
        estimated_size_bytes=estimated_size,
        free_disk_bytes=free_disk,
        command=command,
        selected_quant=selected_quant,
        selected_files=selected_files,
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
    print("\n" + "\n".join(plan.next_steps()))

    return plan
