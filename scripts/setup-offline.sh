#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
synth_root="$(cd "${script_dir}/.." && pwd)"
uv_lock_path="${synth_root}/uv.lock"
tsx_path="${synth_root}/node_modules/.bin/tsx"
overlay_python="${synth_root}/.venv/bin/python"

for required_command in node npm uv fd rg; do
	if ! command -v "${required_command}" >/dev/null 2>&1; then
		printf 'setup-offline: %s is required before setup\n' "${required_command}" >&2
		exit 1
	fi
done
if [[ -n "${PRIME_AGENT_KERNEL_PYTHON:-}" ]]; then
	printf 'setup-offline: unset PRIME_AGENT_KERNEL_PYTHON; Synth provisions a separate managed kernel venv\n' >&2
	exit 1
fi
for required_file in \
	"${uv_lock_path}" \
	"${synth_root}/.prime/agent/models.json" \
	"${script_dir}/bootstrap-synth-kernel.ts" \
	"${script_dir}/merge-local-models.mjs"; do
	if [[ ! -f "${required_file}" ]]; then
		printf 'setup-offline: required tracked file is missing: %s\n' "${required_file}" >&2
		exit 1
	fi
done

printf 'setup-offline: pre-run receipt node_modules=%s overlay_venv=%s kernel_override=%s\n' \
	"$([[ -d "${synth_root}/node_modules" ]] && printf present || printf absent)" \
	"$([[ -x "${overlay_python}" ]] && printf present || printf absent)" \
	"${PRIME_AGENT_KERNEL_VENV:-default}"
printf 'setup-offline: node=%s npm=%s uv=%s fd=%s rg=%s\n' \
	"$(node --version)" \
	"$(npm --version)" \
	"$(uv --version | awk '{print $2}')" \
	"$(fd --version | awk 'NR == 1 {print $2}')" \
	"$(rg --version | awk 'NR == 1 {print $2}')"

(
	cd "${synth_root}"
	npm_config_update_notifier=false npm install --no-audit --no-fund
)
if [[ ! -x "${tsx_path}" ]]; then
	printf 'setup-offline: npm install did not provision %s\n' "${tsx_path}" >&2
	exit 1
fi

uv python install 3.12 --managed-python
UV_PROJECT_ENVIRONMENT="${synth_root}/.venv" \
	uv sync \
		--project "${synth_root}" \
		--python 3.12 \
		--managed-python \
		--locked \
		--extra dev
if [[ ! -x "${overlay_python}" ]]; then
	printf 'setup-offline: locked overlay Python was not created at %s\n' "${overlay_python}" >&2
	exit 1
fi
overlay_version="$(${overlay_python} -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${overlay_version}" != "3.12" ]]; then
	printf 'setup-offline: expected overlay Python 3.12, got %s\n' "${overlay_version}" >&2
	exit 1
fi

(
	cd "${synth_root}"
	PRIME_AGENT_INSTALL_UV=0 "${tsx_path}" "${script_dir}/bootstrap-synth-kernel.ts" "${synth_root}"
)

embed_cache_dir="${SYNTH_FASTEMBED_CACHE_DIR:-${FASTEMBED_CACHE_PATH:-${synth_root}/.synth/models/fastembed}}"
mkdir -p "${embed_cache_dir}"
SYNTH_FASTEMBED_CACHE_DIR="${embed_cache_dir}" "${overlay_python}" - <<'PY'
import os

from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EXPECTED_DIMENSION = 384
cache_dir = os.environ["SYNTH_FASTEMBED_CACHE_DIR"]

embedding = TextEmbedding(model_name=MODEL_NAME, cache_dir=cache_dir)
dimension = len(next(embedding.embed(["Synth offline setup"])))
if dimension != EXPECTED_DIMENSION:
    raise SystemExit(
        f"setup-offline: {MODEL_NAME} dimension {dimension}, expected {EXPECTED_DIMENSION}"
    )

offline_embedding = TextEmbedding(
    model_name=MODEL_NAME,
    cache_dir=cache_dir,
    local_files_only=True,
)
offline_dimension = len(next(offline_embedding.embed(["Synth offline verification"])))
if offline_dimension != EXPECTED_DIMENSION:
    raise SystemExit(
        f"setup-offline: offline {MODEL_NAME} dimension {offline_dimension}, "
        f"expected {EXPECTED_DIMENSION}"
    )

print(
    f"setup-offline: embedding model ready name={MODEL_NAME} "
    f"dimension={offline_dimension} cache={cache_dir}"
)
PY

node "${script_dir}/merge-local-models.mjs"
printf 'setup-offline: ready overlay=%s\n' "${overlay_python}"
