"""Integrity manifest for reviewed research prompt ports."""

from __future__ import annotations

from hashlib import sha256
from types import ModuleType

from local_memory import prompts_reasoning_bank
from middleware import prompts_bats

PORT_MODULES: tuple[ModuleType, ...] = (prompts_bats, prompts_reasoning_bank)


def prompt_port_manifest() -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for module in PORT_MODULES:
        for name, value in vars(module).items():
            if not name.isupper() or not isinstance(value, (int, str)):
                continue
            encoded = str(value).encode()
            manifest.append(
                {
                    "module": module.__name__,
                    "name": name,
                    "sha256": sha256(encoded).hexdigest(),
                    "bytes": len(encoded),
                }
            )
    return sorted(
        manifest,
        key=lambda item: (str(item["module"]), str(item["name"])),
    )
