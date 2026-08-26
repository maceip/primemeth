#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
synth_root="$(cd "${script_dir}/.." && pwd)"

cd "${synth_root}"

node --input-type=module - "${synth_root}" <<'NODE'
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, extname, relative, resolve } from "node:path";
import { init, parse } from "es-module-lexer";

await init;

const repositoryRoot = resolve(process.argv[2]);
const extensionRoot = resolve(repositoryRoot, ".prime/agent/extensions/synth");
const manifestPath = resolve(extensionRoot, "package.json");
const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
const entries = manifest?.pi?.extensions;
if (!Array.isArray(entries) || entries.length !== 1 || entries[0] !== "./index.ts") {
	throw new Error("Synth extension manifest must expose exactly ./index.ts");
}

function collectSourceFiles(directory) {
	const files = [];
	for (const name of readdirSync(directory)) {
		const path = resolve(directory, name);
		const stat = statSync(path);
		if (stat.isDirectory()) {
			files.push(...collectSourceFiles(path));
		} else if ([".ts", ".tsx", ".mts", ".cts"].includes(extname(path))) {
			files.push(path);
		}
	}
	return files;
}

const sources = collectSourceFiles(extensionRoot);
const sourceSet = new Set(sources);
const entry = resolve(extensionRoot, entries[0]);
if (!sourceSet.has(entry)) {
	throw new Error(`Synth extension entry is missing: ${entry}`);
}

function resolveRelativeImport(importer, specifier) {
	const absolute = resolve(dirname(importer), specifier);
	const candidates = [];
	if (specifier.endsWith(".js")) {
		candidates.push(absolute.slice(0, -3) + ".ts");
	} else if (specifier.endsWith(".mjs")) {
		candidates.push(absolute.slice(0, -4) + ".mts");
	} else if (specifier.endsWith(".cjs")) {
		candidates.push(absolute.slice(0, -4) + ".cts");
	} else if (extname(specifier)) {
		candidates.push(absolute);
	} else {
		candidates.push(
			`${absolute}.ts`,
			`${absolute}.tsx`,
			resolve(absolute, "index.ts"),
		);
	}
	const match = candidates.find((candidate) => sourceSet.has(candidate));
	if (!match) {
		throw new Error(
			`Unresolved relative import ${specifier} from ${relative(repositoryRoot, importer)}`,
		);
	}
	if (!match.startsWith(`${extensionRoot}/`)) {
		throw new Error(`Extension import escapes its control-plane root: ${specifier}`);
	}
	return match;
}

const reachable = new Set();
const pending = [entry];
while (pending.length > 0) {
	const path = pending.pop();
	if (!path || reachable.has(path)) continue;
	reachable.add(path);
	const source = readFileSync(path, "utf8");
	const specifiers = [];
	const [imports] = parse(source, path);
	for (const imported of imports) {
		if (imported.d >= 0) {
			throw new Error(`Dynamic import is forbidden in ${relative(repositoryRoot, path)}`);
		}
		if (imported.n) specifiers.push(imported.n);
	}
	for (const specifier of specifiers) {
		if (!specifier.startsWith(".")) continue;
		const dependency = resolveRelativeImport(path, specifier);
		if (!reachable.has(dependency)) pending.push(dependency);
	}
}

const orphans = sources.filter((path) => !reachable.has(path));
if (orphans.length > 0) {
	throw new Error(
		`Unreachable Synth extension modules:\n${orphans
			.map((path) => `  ${relative(repositoryRoot, path)}`)
			.join("\n")}`,
	);
}

const reachableText = [...reachable].map((path) => readFileSync(path, "utf8")).join("\n");
if (!reachableText.includes("synth.json")) {
	throw new Error("The extension graph does not own synth.json");
}
if (!reachableText.includes("synth-bridge")) {
	throw new Error("The extension graph does not own the synth-bridge process");
}
if (!existsSync(resolve(extensionRoot, "tsconfig.json"))) {
	throw new Error("Synth extension tsconfig.json is missing");
}

console.log(
	`check-wiring: extension head index.ts reaches ${reachable.size}/${sources.length} TypeScript modules`,
);
NODE

uv run --project "${synth_root}" --no-sync python - "${synth_root}" <<'PYTHON'
from __future__ import annotations

import ast
import sys
import tomllib
from collections import deque
from pathlib import Path

repository_root = Path(sys.argv[1]).resolve()
manifest_path = repository_root / "pyproject.toml"
with manifest_path.open("rb") as stream:
    manifest = tomllib.load(stream)

scripts = manifest.get("project", {}).get("scripts", {})
expected_scripts = {
    "synth": "synth.cli:main",
    "synth-bridge": "synth.cli:bridge_main",
}
if scripts != expected_scripts:
    raise SystemExit(
        "check-wiring: Python console surface must be exactly synth plus the "
        "internal synth-bridge alias"
    )

package_names = manifest["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
if not isinstance(package_names, list) or not all(
    isinstance(name, str) and "." not in name for name in package_names
):
    raise SystemExit("check-wiring: Hatch package roots must be top-level names")

module_paths: dict[str, Path] = {}
for package_name in package_names:
    package_root = repository_root / package_name
    if not package_root.is_dir():
        raise SystemExit(f"check-wiring: package root is missing: {package_name}")
    for path in package_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative_path = path.relative_to(repository_root)
        if path.name == "__init__.py":
            module = ".".join(relative_path.parent.parts)
        else:
            module = ".".join((*relative_path.parent.parts, path.stem))
        if module in module_paths:
            raise SystemExit(f"check-wiring: duplicate Python module: {module}")
        module_paths[module] = path

top_level = set(package_names)
reachable: set[str] = set()
pending: deque[str] = deque(["synth.cli"])


def enqueue(module: str) -> None:
    candidate = module
    found = False
    while candidate:
        if candidate in module_paths:
            found = True
            if candidate not in reachable:
                pending.append(candidate)
        candidate = candidate.rpartition(".")[0]
    if not found and module.partition(".")[0] in top_level:
        raise SystemExit(f"check-wiring: unresolved local Python import: {module}")


while pending:
    module = pending.popleft()
    if module in reachable:
        continue
    reachable.add(module)
    path = module_paths.get(module)
    if path is None:
        raise SystemExit(f"check-wiring: missing Python module: {module}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    current_package = module.split(".") if is_package else module.split(".")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            is_dunder_import = isinstance(node.func, ast.Name) and node.func.id == "__import__"
            is_import_module = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
            if is_dunder_import or is_import_module:
                relative_path = path.relative_to(repository_root)
                raise SystemExit(
                    f"check-wiring: dynamic Python import is forbidden: {relative_path}:{node.lineno}"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.partition(".")[0] in top_level:
                    enqueue(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                parents = node.level - 1
                if parents > len(current_package):
                    raise SystemExit(
                        f"check-wiring: invalid relative import in {path}:{node.lineno}"
                    )
                prefix = current_package[: len(current_package) - parents]
                base = ".".join((*prefix, *(node.module or "").split(".")))
                base = base.strip(".")
            else:
                base = node.module or ""
            if not base or base.partition(".")[0] not in top_level:
                continue
            enqueue(base)
            for alias in node.names:
                if alias.name != "*":
                    candidate = f"{base}.{alias.name}"
                    if candidate in module_paths:
                        enqueue(candidate)

orphans = sorted(set(module_paths) - reachable)
if orphans:
    formatted = "\n".join(f"  {module_paths[name].relative_to(repository_root)}" for name in orphans)
    raise SystemExit(f"check-wiring: unreachable Python modules:\n{formatted}")

print(
    "check-wiring: CLI head synth.cli:main reaches "
    f"{len(reachable)}/{len(module_paths)} Python modules; "
    "synth-bridge is an internal alias"
)
PYTHON

npx tsgo --noEmit -p .prime/agent/extensions/synth/tsconfig.json

printf 'check-wiring: nonruntime setup, validation, and demo scripts classified as drivers\n'
printf 'check-wiring: promoted .prime/agent/skills packages classified as data-plane loader inputs\n'
printf 'check-wiring: PASS\n'
