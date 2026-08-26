#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cli_path="${repo_root}/packages/coding-agent/dist/cli.js"
fixture_path="${repo_root}/packages/coding-agent/test/fixtures/eng-4600-faux-extension.ts"

for required_command in node perl; do
	if ! command -v "${required_command}" >/dev/null 2>&1; then
		printf 'proof-faux-print: %s is required\n' "${required_command}" >&2
		exit 1
	fi
done
if [[ ! -f "${cli_path}" ]]; then
	printf 'proof-faux-print: built CLI missing at %s; run npm run build first\n' "${cli_path}" >&2
	exit 1
fi
if [[ ! -f "${fixture_path}" ]]; then
	printf 'proof-faux-print: faux extension fixture missing at %s\n' "${fixture_path}" >&2
	exit 1
fi

proof_dir="$(mktemp -d /tmp/synth-proof-faux-print.XXXXXX)"
proof_agent_dir="${proof_dir}/agent"
proof_socket="${proof_dir}/daemon.sock"
proof_stdout="${proof_dir}/stdout.txt"
proof_stderr="${proof_dir}/stderr.txt"
proof_shutdown_complete=0
mkdir -p "${proof_agent_dir}" "${proof_dir}/xdg-cache" "${proof_dir}/xdg-config" "${proof_dir}/xdg-data"

proof_environment=(
	"PATH=${PATH}"
	"HOME=${HOME}"
	"SHELL=${SHELL:-/bin/sh}"
	"TERM=${TERM:-xterm-256color}"
	"LANG=${LANG:-C}"
	"LC_ALL=${LC_ALL:-}"
	"LC_CTYPE=${LC_CTYPE:-}"
	"PRIME_AGENT_CODING_AGENT_DIR=${proof_agent_dir}"
	"PRIME_AGENT_INTERNAL_DAEMON_SUPERVISOR_REGISTRY_DIR=${proof_dir}/supervisor-owners"
	"TMPDIR=${proof_dir}"
	"XDG_CACHE_HOME=${proof_dir}/xdg-cache"
	"XDG_CONFIG_HOME=${proof_dir}/xdg-config"
	"XDG_DATA_HOME=${proof_dir}/xdg-data"
	"PI_OFFLINE=1"
	"PI_SKIP_VERSION_CHECK=1"
	"DO_NOT_TRACK=1"
	"PRIME_AGENT_TELEMETRY=0"
	"HTTP_PROXY=http://127.0.0.1:9"
	"HTTPS_PROXY=http://127.0.0.1:9"
	"ALL_PROXY=http://127.0.0.1:9"
	"http_proxy=http://127.0.0.1:9"
	"https_proxy=http://127.0.0.1:9"
	"all_proxy=http://127.0.0.1:9"
	"NO_PROXY=localhost,127.0.0.1,::1"
	"no_proxy=localhost,127.0.0.1,::1"
)

shutdown_private_daemon() {
	(
		cd "${repo_root}"
		/usr/bin/env -i "${proof_environment[@]}" node --input-type=module --eval '
import { shutdownDaemonAndWait } from "./packages/coding-agent/dist/cli/daemon-launch.js";

if (!(await shutdownDaemonAndWait(process.argv[1], 10_000))) {
	process.exit(1);
}
' "${proof_socket}"
	)
}

cleanup() {
	local proof_exit_code=$?
	local proof_preserve_dir=0
	trap - EXIT
	set +e
	if [[ "${proof_shutdown_complete}" -eq 0 ]]; then
		if ! shutdown_private_daemon >/dev/null 2>&1 || [[ -e "${proof_socket}" ]]; then
			printf 'proof-faux-print: could not prove private daemon shutdown; preserving %s\n' "${proof_dir}" >&2
			proof_exit_code=1
			proof_preserve_dir=1
		fi
	fi
	if [[ "${proof_exit_code}" -ne 0 ]]; then
		for log_file in "${proof_agent_dir}"/logs/*.log; do
			[[ -f "${log_file}" ]] || continue
			printf 'proof-faux-print: daemon log %s\n' "${log_file}" >&2
			sed -n '1,240p' "${log_file}" >&2
		done
	fi
	if [[ "${proof_preserve_dir}" -eq 0 ]]; then
		case "${proof_dir}" in
			/tmp/synth-proof-faux-print.*) rm -rf -- "${proof_dir}" ;;
			*)
				printf 'proof-faux-print: refusing to remove unexpected path %s\n' "${proof_dir}" >&2
				proof_exit_code=1
				;;
		esac
	fi
	exit "${proof_exit_code}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

proof_process_exit=0

/usr/bin/env -i "${proof_environment[@]}" node --input-type=module --eval '
const allowed = new Set([
  "PATH", "HOME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
  "PRIME_AGENT_CODING_AGENT_DIR", "PRIME_AGENT_INTERNAL_DAEMON_SUPERVISOR_REGISTRY_DIR",
  "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PI_OFFLINE",
  "PI_SKIP_VERSION_CHECK", "DO_NOT_TRACK", "PRIME_AGENT_TELEMETRY", "HTTP_PROXY",
  "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
  "__CF_USER_TEXT_ENCODING",
]);
const unexpected = Object.keys(process.env).filter((name) => !allowed.has(name)).sort();
if (unexpected.length > 0) throw new Error(`unexpected environment names: ${unexpected.join(", ")}`);
'
(
	cd "${repo_root}"
	/usr/bin/perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!\n"' \
		60 \
		/usr/bin/env -i "${proof_environment[@]}" \
		node "${cli_path}" \
			--offline \
			--cwd "${repo_root}" \
			--daemon-socket "${proof_socket}" \
			--model faux/faux \
			--extension "${fixture_path}" \
			--no-tools \
			--no-skills \
			--no-prompt-templates \
			--no-themes \
			--no-context-files \
			--no-session \
			--print "Synth faux print proof"
) >"${proof_stdout}" 2>"${proof_stderr}" || proof_process_exit=$?

if [[ "${proof_process_exit}" -ne 0 ]]; then
	sed -n '1,240p' "${proof_stdout}" >&2
	sed -n '1,240p' "${proof_stderr}" >&2
	printf 'proof-faux-print: CLI exited %s\n' "${proof_process_exit}" >&2
	exit 1
fi
if [[ -s "${proof_stderr}" ]]; then
	sed -n '1,240p' "${proof_stderr}" >&2
	printf 'proof-faux-print: CLI emitted stderr\n' >&2
	exit 1
fi
if ! diff -u <(printf 'upgrade response 1\n') "${proof_stdout}"; then
	printf 'proof-faux-print: stdout did not exactly match the faux response\n' >&2
	exit 1
fi
if [[ ! -S "${proof_socket}" ]]; then
	printf 'proof-faux-print: private daemon socket was not observed\n' >&2
	exit 1
fi

sed -n '1p' "${proof_stdout}"
if ! shutdown_private_daemon; then
	printf 'proof-faux-print: private daemon shutdown failed\n' >&2
	exit 1
fi
if [[ -e "${proof_socket}" ]]; then
	printf 'proof-faux-print: private daemon socket remains after shutdown\n' >&2
	exit 1
fi
proof_shutdown_complete=1

printf 'proof-faux-print: PASS\n'
