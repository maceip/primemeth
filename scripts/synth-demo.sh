#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
synth_root="$(cd "${script_dir}/.." && pwd)"
catalog_path="${synth_root}/packages/ai/src/models.generated.ts"
ai_manifest_path="${synth_root}/packages/ai/package.json"

for required_command in git node npm; do
	if ! command -v "${required_command}" >/dev/null 2>&1; then
		printf 'synth-demo: %s is required\n' "${required_command}" >&2
		exit 1
	fi
done
if [[ ! -f "${catalog_path}" ]]; then
	printf 'synth-demo: pinned catalog missing at %s\n' "${catalog_path}" >&2
	exit 1
fi
node --input-type=module --eval '
import { readFileSync } from "node:fs";

const manifest = JSON.parse(readFileSync(process.argv[1], "utf8"));
const scripts = manifest.scripts ?? {};
if (scripts["generate-models"] !== "npx tsx scripts/generate-models.ts") {
	console.error("synth-demo: explicit model-catalog generator changed or is missing");
	process.exit(1);
}
if (scripts.build !== "tsgo -p tsconfig.build.json") {
	console.error("synth-demo: package build does not use the pinned-catalog command");
	process.exit(1);
}
for (const hook of ["prebuild", "postbuild"]) {
	if (Object.hasOwn(scripts, hook)) {
		console.error(`synth-demo: package ${hook} hook bypasses the pinned-build contract`);
		process.exit(1);
	}
}
' "${ai_manifest_path}"
if ! git -C "${synth_root}" diff --quiet -- "${catalog_path}" || \
	! git -C "${synth_root}" diff --cached --quiet -- "${catalog_path}"; then
	printf 'synth-demo: refusing to build with a modified pinned catalog\n' >&2
	exit 1
fi

catalog_hash_before="$(git -C "${synth_root}" hash-object "${catalog_path}")"
(
	cd "${synth_root}"
	PI_OFFLINE=1 npm run build
)
catalog_hash_after="$(git -C "${synth_root}" hash-object "${catalog_path}")"

if [[ "${catalog_hash_before}" != "${catalog_hash_after}" ]]; then
	printf 'synth-demo: build changed the pinned model catalog\n' >&2
	exit 1
fi
if ! git -C "${synth_root}" diff --quiet -- "${catalog_path}" || \
	! git -C "${synth_root}" diff --cached --quiet -- "${catalog_path}"; then
	printf 'synth-demo: build left a pinned-catalog worktree change\n' >&2
	exit 1
fi

printf 'synth-demo: Phase 1 A3 pinned-catalog build PASS (%s)\n' "${catalog_hash_after}"
