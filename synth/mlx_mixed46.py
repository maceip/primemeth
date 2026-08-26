"""Mixed-precision MLX convert hook for Qwen3.8-Flash-Next.

mlx_lm.convert --quant-predicate only accepts named recipes (mixed_2_6,
mixed_3_4, mixed_3_6, mixed_4_6). Those recipes pick bits by layer index
(v_proj / down_proj / lm_head), not by experts vs attention. This hook is
the actual mixed46 recipe and is executed by the mlx-lm interpreter.
"""

from __future__ import annotations

import argparse
import sys

EXPERT_BITS = 4
EXPERT_GROUP_SIZE = 32
HIGH_BITS = 6
HIGH_GROUP_SIZE = 64
QUANT_MODE = "affine"

DEFAULT_INPUT = "/Users/mac/models/Qwen3.8-Flash-Next"
DEFAULT_OUTPUT = "/Users/mac/models/Qwen3.8-Flash-Next-MLX-mixed46"

_EXPERT_MARKERS = (
    "experts",
    "switch_mlp",
    "shared_expert",
    "shared_mlp",
)
_ROUTER_SUFFIXES = ("mlp.gate", "shared_expert_gate")
_EMBED_MARKERS = (
    "embed",
    "lm_head",
    "wte",
    "tok_embeddings",
    "ngram",
)
_ATTENTION_MARKERS = (
    "self_attn",
    "attention",
    "linear_attn",
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "qkv_proj",
    "q_a_proj",
    "q_b_proj",
    "kv_a_proj",
    "kv_b_proj",
    "in_proj_qkv",
    "in_proj_z",
    "dt_proj",
    "indexer",
)


def recipe_as_dict() -> dict[str, object]:
    return {
        "name": "mixed46",
        "experts_moe": {
            "bits": EXPERT_BITS,
            "group_size": EXPERT_GROUP_SIZE,
            "mode": QUANT_MODE,
        },
        "attention": {
            "bits": HIGH_BITS,
            "group_size": HIGH_GROUP_SIZE,
            "mode": QUANT_MODE,
        },
        "embeddings": {
            "bits": HIGH_BITS,
            "group_size": HIGH_GROUP_SIZE,
            "mode": QUANT_MODE,
        },
        "router": {
            "bits": HIGH_BITS,
            "group_size": HIGH_GROUP_SIZE,
            "mode": QUANT_MODE,
        },
        "mlx_lm_named_recipe": None,
        "reason": (
            "mlx_lm.convert --quant-predicate mixed_4_6 is layer-index based, "
            "not expert-vs-attention. This Python hook is the recipe."
        ),
    }


def _is_expert(path: str) -> bool:
    lowered = path.lower()
    if lowered.endswith("shared_expert_gate"):
        return False
    return any(marker in lowered for marker in _EXPERT_MARKERS)


def _is_router(path: str) -> bool:
    lowered = path.lower()
    return lowered.endswith(_ROUTER_SUFFIXES) or ".router" in lowered


def _is_embedding(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in _EMBED_MARKERS)


def _is_attention(path: str) -> bool:
    if _is_expert(path):
        return False
    lowered = path.lower()
    return any(marker in lowered for marker in _ATTENTION_MARKERS)


def mixed46_params(path: str) -> dict[str, object]:
    if _is_router(path) or _is_embedding(path) or _is_attention(path):
        return {
            "group_size": HIGH_GROUP_SIZE,
            "bits": HIGH_BITS,
            "mode": QUANT_MODE,
        }
    return {
        "group_size": EXPERT_GROUP_SIZE,
        "bits": EXPERT_BITS,
        "mode": QUANT_MODE,
    }


def mixed46_predicate(path: str, _module: object) -> dict[str, object]:
    params = mixed46_params(path)
    bits = params["bits"]
    group_size = params["group_size"]
    print(f"[quantize] {path} bits={bits} group_size={group_size}", flush=True)
    return params


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Hugging Face weights to mixed46 MLX format"
    )
    parser.add_argument("--hf-path", required=True)
    parser.add_argument("--mlx-path", required=True)
    parser.add_argument("--dtype", default="bfloat16")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        from mlx_lm.convert import convert  # type: ignore[import-not-found]
    except ImportError as error:
        print(
            f"mlx_lm.convert is required in this interpreter: {error}",
            file=sys.stderr,
        )
        return 2
    convert(
        hf_path=args.hf_path,
        mlx_path=args.mlx_path,
        quantize=True,
        q_group_size=EXPERT_GROUP_SIZE,
        q_bits=EXPERT_BITS,
        q_mode=QUANT_MODE,
        dtype=args.dtype,
        quant_predicate=mixed46_predicate,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
