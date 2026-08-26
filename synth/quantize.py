"""Wrap mlx_lm.convert with the mixed46 predicate and shard completeness checks."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import cast

from synth.engine import (
    EngineError,
    mlx_model_type,
    require_mlx_model_support,
    resolve_mlx_python,
)
from synth.mlx_mixed46 import (
    DEFAULT_INPUT,
    DEFAULT_OUTPUT,
    mixed46_params,
    recipe_as_dict,
)

_SHARD_RE = re.compile(
    r"^(?P<stem>.+)-(?P<index>\d+)-of-(?P<total>\d+)\.(?P<ext>safetensors|bin)$"
)
_INDEX_NAMES = (
    "model.safetensors.index.json",
    "model.safetensors.index.json.gz",
    "pytorch_model.bin.index.json",
)


class QuantizeError(RuntimeError):
    """Raised when mixed46 conversion cannot start."""


def _index_path(model_dir: Path) -> Path | None:
    for name in _INDEX_NAMES:
        candidate = model_dir / name
        if candidate.is_file():
            return candidate
    matches = sorted(model_dir.glob("*.safetensors.index.json"))
    return matches[0] if matches else None


def _shards_from_names(model_dir: Path) -> tuple[int, int, str]:
    totals: set[int] = set()
    present: set[tuple[str, int]] = set()
    example = ""
    for path in model_dir.iterdir():
        match = _SHARD_RE.match(path.name)
        if match is None:
            continue
        total = int(match.group("total"))
        index = int(match.group("index"))
        totals.add(total)
        present.add((match.group("stem"), index))
        if not example:
            example = path.name
    if not present:
        return 0, 0, example
    expected = max(totals)
    return len({index for _, index in present}), expected, example


def _shards_from_index(index_path: Path) -> list[str]:
    try:
        raw: object = json.loads(index_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise QuantizeError(
            f"cannot read weight index {index_path}: {error}"
        ) from error
    if not isinstance(raw, dict):
        raise QuantizeError(f"weight index must be an object: {index_path}")
    weight_map = raw.get("weight_map")
    if not isinstance(weight_map, dict):
        raise QuantizeError(f"weight index is missing weight_map: {index_path}")
    files: list[str] = []
    seen: set[str] = set()
    for value in weight_map.values():
        if not isinstance(value, str) or not value:
            raise QuantizeError(
                f"weight index has a non-string shard name: {index_path}"
            )
        if value not in seen:
            seen.add(value)
            files.append(value)
    return files


def check_complete(model_dir: Path) -> dict[str, object]:
    if not model_dir.is_dir():
        raise QuantizeError(f"input is not a directory: {model_dir}")
    index = _index_path(model_dir)
    present_count, expected_count, example = _shards_from_names(model_dir)
    missing: list[str] = []
    if index is not None:
        expected_files = _shards_from_index(index)
        missing = [name for name in expected_files if not (model_dir / name).is_file()]
        return {
            "index": str(index),
            "expected": len(expected_files),
            "present": len(expected_files) - len(missing),
            "missing": missing,
            "complete": not missing,
        }
    if expected_count > 0 and present_count < expected_count:
        missing = [
            f"shard files {present_count} of {expected_count}"
            + (f" ({example})" if example else "")
        ]
        return {
            "index": None,
            "expected": expected_count,
            "present": present_count,
            "missing": [
                "model.safetensors.index.json",
                *missing,
            ],
            "complete": False,
        }
    if expected_count > 0 and present_count == expected_count:
        return {
            "index": None,
            "expected": expected_count,
            "present": present_count,
            "missing": ["model.safetensors.index.json"],
            "complete": False,
        }
    single = model_dir / "model.safetensors"
    if single.is_file():
        return {
            "index": None,
            "expected": 1,
            "present": 1,
            "missing": [],
            "complete": True,
        }
    raise QuantizeError(
        f"input download is incomplete at {model_dir}: missing "
        "model.safetensors.index.json and no complete shard set"
    )


def _refuse_incomplete(model_dir: Path, report: dict[str, object]) -> None:
    if report.get("complete") is True:
        return
    missing = report.get("missing")
    names = ", ".join(cast(list[str], missing)[:8]) if isinstance(missing, list) else ""
    present = report.get("present")
    expected = report.get("expected")
    raise QuantizeError(
        f"input download is incomplete at {model_dir}: "
        f"present {present} of {expected} shards"
        + (f"; missing {names}" if names else "")
        + ". Finish the download before quantizing."
    )


def convert_argv(input_dir: Path, output_dir: Path) -> list[str]:
    hook = Path(__file__).with_name("mlx_mixed46.py").resolve()
    try:
        python = resolve_mlx_python()
    except EngineError as error:
        raise QuantizeError(str(error)) from error
    return [
        *python,
        str(hook),
        "--hf-path",
        str(input_dir),
        "--mlx-path",
        str(output_dir),
        "--dtype",
        "bfloat16",
    ]


def plan(
    input_dir: Path,
    output_dir: Path,
) -> dict[str, object]:
    completeness = check_complete(input_dir)
    support_error: str | None = None
    model_type = mlx_model_type(input_dir)
    try:
        require_mlx_model_support(input_dir)
    except EngineError as error:
        support_error = str(error)
    return {
        "input": str(input_dir),
        "output": str(output_dir),
        "dtype": "bfloat16",
        "recipe": recipe_as_dict(),
        "example": mixed46_params("model.layers.0.mlp.switch_mlp.down_proj"),
        "completeness": completeness,
        "mlxSupport": {
            "modelType": model_type,
            "supported": support_error is None,
            "error": support_error,
        },
        "argv": convert_argv(input_dir, output_dir),
    }


def print_recipe(resolved: dict[str, object]) -> None:
    recipe = cast(dict[str, object], resolved["recipe"])
    experts = cast(dict[str, object], recipe["experts_moe"])
    attention = cast(dict[str, object], recipe["attention"])
    embeddings = cast(dict[str, object], recipe["embeddings"])
    router = cast(dict[str, object], recipe["router"])
    completeness = cast(dict[str, object], resolved["completeness"])
    mlx_support = cast(dict[str, object], resolved["mlxSupport"])
    argv = cast(list[str], resolved["argv"])
    print("quantize recipe: mixed46")
    print(f"  input: {resolved['input']}")
    print(f"  output: {resolved['output']}")
    print(f"  dtype: {resolved['dtype']}")
    print(
        "  experts/MoE: "
        f"{experts['bits']}-bit group_size={experts['group_size']} {experts['mode']}"
    )
    print(
        "  attention: "
        f"{attention['bits']}-bit group_size={attention['group_size']} "
        f"{attention['mode']}"
    )
    print(
        "  embeddings: "
        f"{embeddings['bits']}-bit group_size={embeddings['group_size']} "
        f"{embeddings['mode']}"
    )
    print(
        "  router: "
        f"{router['bits']}-bit group_size={router['group_size']} {router['mode']}"
    )
    print(f"  mlx_lm named recipe: {recipe['mlx_lm_named_recipe']}")
    print(f"  reason: {recipe['reason']}")
    print(
        "  completeness: "
        f"present {completeness.get('present')} of {completeness.get('expected')} "
        f"complete={completeness.get('complete')}"
    )
    missing = completeness.get("missing")
    if isinstance(missing, list) and missing:
        print(f"  missing: {', '.join(str(item) for item in missing[:12])}")
    print(
        "  mlx-lm support: "
        f"model_type={mlx_support['modelType']} "
        f"supported={mlx_support['supported']}"
    )
    if mlx_support.get("error"):
        print(f"  mlx-lm blocker: {mlx_support['error']}")
    print(f"  convert: {' '.join(argv)}")


def run_quantize(
    input_dir: Path,
    output_dir: Path,
    *,
    dry_run: bool,
) -> int:
    resolved_input = input_dir.expanduser().resolve()
    resolved_output = output_dir.expanduser().resolve()
    resolved = plan(resolved_input, resolved_output)
    print_recipe(resolved)
    if dry_run:
        return 0
    _refuse_incomplete(
        resolved_input, cast(dict[str, object], resolved["completeness"])
    )
    mlx_support = cast(dict[str, object], resolved["mlxSupport"])
    if mlx_support.get("supported") is not True:
        raise QuantizeError(str(mlx_support.get("error") or "mlx-lm is unsupported"))
    if resolved_output.exists():
        raise QuantizeError(
            f"output already exists: {resolved_output}. "
            "Delete it or choose another path."
        )
    argv = cast(list[str], resolved["argv"])
    print(f"running: {' '.join(argv)}", flush=True)
    try:
        completed = subprocess.run(argv, check=False)
    except FileNotFoundError as error:
        raise QuantizeError(f"failed to launch convert: {error}") from error
    if completed.returncode != 0:
        raise QuantizeError(f"mlx_lm convert exited {completed.returncode}")
    return 0


def default_input() -> Path:
    return Path(DEFAULT_INPUT)


def default_output() -> Path:
    return Path(DEFAULT_OUTPUT)
