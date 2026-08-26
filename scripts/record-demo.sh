#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cli_path="${repo_root}/packages/coding-agent/dist/bundle/cli.js"
demo_mode="${SYNTH_DEMO_MODE:-local}"
demo_endpoint="${SYNTH_DEMO_ENDPOINT:-http://127.0.0.1:8080/v1}"
demo_model="${SYNTH_DEMO_MODEL:-qwen3.8-flash-next}"
demo_prompt="${SYNTH_DEMO_PROMPT:-Audit the Synth telemetry spine for one concrete reliability risk. Trace it through the TypeScript extension and Python mirror, run the narrow regression test, and report evidence without editing files.}"

# RECORDING STAGE 1: start capture before launching this script. The Prime METH
# scanline boot runs for 1.92 seconds; any key skips it without entering text.
# RECORDING STAGE 2: show `/synth budget` so the transcript visibly contains the
# exact <budget> block and current regime.
# RECORDING STAGE 3: submit the real repository audit prompt above. Keep the
# footer in frame through the response so prefill/decode tok/s appear at turn end.
# RECORDING STAGE 4: hold on the populated rail, then type `/quit`.

if [[ "${demo_mode}" == "faux" ]]; then
	printf 'record-demo: faux rehearsal; evidence will be written under .synth/evidence/item4\n'
	SYNTH_TUI_PROMPT="${demo_prompt}" exec "${script_dir}/proof-faux-tui.sh"
fi

if [[ "${demo_mode}" != "local" ]]; then
	printf 'record-demo: SYNTH_DEMO_MODE must be local or faux\n' >&2
	exit 2
fi
if ! DEMO_ENDPOINT="${demo_endpoint}" node --input-type=module --eval '
const endpoint = process.env.DEMO_ENDPOINT;
try {
  if (!endpoint) process.exit(1);
  const parsed = new URL(endpoint);
  const allowedHosts = new Set(["localhost", "127.0.0.1", "[::1]"]);
  if (
    parsed.protocol !== "http:" ||
    !allowedHosts.has(parsed.hostname) ||
    parsed.username !== "" ||
    parsed.password !== ""
  ) process.exit(1);
} catch {
  process.exit(1);
}
'; then
	printf 'record-demo: endpoint must be loopback: %s\n' "${demo_endpoint}" >&2
	exit 2
fi

demo_dir="$(mktemp -d /tmp/synth-record-demo.XXXXXX)"
demo_agent_dir="${demo_dir}/agent"
demo_socket="${demo_dir}/daemon.sock"
demo_shutdown_complete=0
demo_preserve_dir=0
mkdir -p "${demo_agent_dir}" "${demo_dir}/xdg-cache" "${demo_dir}/xdg-config" "${demo_dir}/xdg-data"

demo_environment=(
	"PATH=${PATH}"
	"HOME=${HOME}"
	"SHELL=${SHELL:-/bin/sh}"
	"TERM=${TERM:-xterm-256color}"
	"COLORTERM=${COLORTERM:-}"
	"LANG=${LANG:-C}"
	"LC_ALL=${LC_ALL:-}"
	"LC_CTYPE=${LC_CTYPE:-}"
	"TMPDIR=${demo_dir}"
	"XDG_CACHE_HOME=${demo_dir}/xdg-cache"
	"XDG_CONFIG_HOME=${demo_dir}/xdg-config"
	"XDG_DATA_HOME=${demo_dir}/xdg-data"
	"NO_PROXY=localhost,127.0.0.1,::1"
	"no_proxy=localhost,127.0.0.1,::1"
)
if [[ -n "${PRIME_AGENT_KERNEL_PYTHON:-}" ]]; then
	demo_environment+=("PRIME_AGENT_KERNEL_PYTHON=${PRIME_AGENT_KERNEL_PYTHON}")
fi

run_isolated() {
	/usr/bin/env -i "${demo_environment[@]}" "$@"
}

shutdown_recording_daemon() {
	run_isolated \
		PRIME_AGENT_CODING_AGENT_DIR="${demo_agent_dir}" \
		PRIME_AGENT_INTERNAL_DAEMON_SUPERVISOR_REGISTRY_DIR="${demo_dir}/supervisor-owners" \
		PI_OFFLINE=1 \
		node --input-type=module --eval '
import { shutdownDaemonAndWait } from "./packages/coding-agent/dist/cli/daemon-launch.js";

if (!(await shutdownDaemonAndWait(process.argv[1], 10_000))) process.exit(1);
' "${demo_socket}"
}

cleanup() {
	local exit_code=$?
	trap - EXIT
	if [[ "${demo_shutdown_complete}" -eq 0 ]] && \
		{ [[ -e "${demo_socket}" ]] || [[ -S "${demo_socket}" ]]; }; then
		if ! shutdown_recording_daemon || [[ -e "${demo_socket}" ]]; then
			printf 'record-demo: private daemon shutdown was not proved; preserving %s\n' "${demo_dir}" >&2
			exit_code=1
			demo_preserve_dir=1
		fi
	fi
	if [[ "${demo_preserve_dir}" -eq 1 ]]; then
		exit "${exit_code}"
	fi
	case "${demo_dir}" in
		/tmp/synth-record-demo.*) rm -rf -- "${demo_dir}" ;;
		*)
			printf 'record-demo: refusing to remove unexpected path %s\n' "${demo_dir}" >&2
			exit_code=1
			;;
	esac
	exit "${exit_code}"
}
trap cleanup EXIT

run_isolated node --input-type=module --eval '
const allowed = new Set([
  "PATH", "HOME", "SHELL", "TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
  "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "NO_PROXY", "no_proxy",
  "PRIME_AGENT_KERNEL_PYTHON", "__CF_USER_TEXT_ENCODING",
]);
const unexpected = Object.keys(process.env).filter((name) => !allowed.has(name)).sort();
if (unexpected.length > 0) throw new Error(`unexpected environment names: ${unexpected.join(", ")}`);
if (process.env.NO_PROXY !== "localhost,127.0.0.1,::1") throw new Error("NO_PROXY is not loopback-only");
'
if [[ "${SYNTH_DEMO_CHECK_ENV_ONLY:-0}" == "1" ]]; then
	printf 'record-demo: environment isolation PASS\n'
	exit 0
fi

if [[ ! -f "${cli_path}" ]]; then
	printf 'record-demo: built CLI missing at %s; run npm run build first\n' "${cli_path}" >&2
	exit 1
fi

run_isolated \
	DEMO_AGENT_DIR="${demo_agent_dir}" \
	DEMO_ENDPOINT="${demo_endpoint}" \
	DEMO_MODEL="${demo_model}" \
	node --input-type=module --eval '
import { writeFileSync } from "node:fs";
import { join } from "node:path";

const agentDir = process.env.DEMO_AGENT_DIR;
const endpoint = process.env.DEMO_ENDPOINT;
const model = process.env.DEMO_MODEL;
if (!agentDir || !endpoint || !model) throw new Error("record-demo configuration is incomplete");
writeFileSync(join(agentDir, "models.json"), `${JSON.stringify({
  providers: {
    "recording-local": {
      baseUrl: endpoint,
      api: "openai-completions",
      apiKey: "SYNTH_LOCAL_API_KEY",
      compat: { supportsDeveloperRole: false, supportsReasoningEffort: false },
      models: [{ id: model, name: model, reasoning: true, contextWindow: 262144 }],
    },
  },
}, null, 2)}\n`);
writeFileSync(join(agentDir, "settings.json"), `${JSON.stringify({
  defaultProvider: "recording-local",
  defaultModel: model,
  telemetry: { enabled: false },
}, null, 2)}\n`);
'

printf 'record-demo: local endpoint %s\n' "${demo_endpoint}"
printf 'record-demo: model recording-local/%s\n' "${demo_model}"
printf 'record-demo: after boot, run /synth budget\n'
printf 'record-demo: then submit: %s\n' "${demo_prompt}"
printf 'record-demo: press Enter to launch the TUI\n'
read -r _

cd "${repo_root}"
run_isolated \
	PRIME_AGENT_CODING_AGENT_DIR="${demo_agent_dir}" \
	PRIME_AGENT_INTERNAL_DAEMON_SUPERVISOR_REGISTRY_DIR="${demo_dir}/supervisor-owners" \
	PI_OFFLINE=1 \
	PI_SKIP_VERSION_CHECK=1 \
	DO_NOT_TRACK=1 \
	PRIME_AGENT_TELEMETRY=0 \
	COLORTERM=truecolor \
	SYNTH_LOCAL_API_KEY="${SYNTH_LOCAL_API_KEY:-local}" \
	node "${cli_path}" \
		--offline \
		--cwd "${repo_root}" \
		--daemon-socket "${demo_socket}" \
		--model "recording-local/${demo_model}"

if ! shutdown_recording_daemon; then
	printf 'record-demo: private daemon shutdown failed; preserving %s\n' "${demo_dir}" >&2
	demo_preserve_dir=1
	exit 1
fi
if [[ -e "${demo_socket}" ]]; then
	printf 'record-demo: private daemon socket remains; preserving %s\n' "${demo_dir}" >&2
	demo_preserve_dir=1
	exit 1
fi
demo_shutdown_complete=1
