#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"
cli_path="${repo_root}/packages/coding-agent/dist/bundle/cli.js"
fixture_path="${repo_root}/packages/coding-agent/test/fixtures/eng-4600-faux-extension.ts"
evidence_root="${SYNTH_TUI_EVIDENCE_DIR:-${repo_root}/.synth/evidence/item4}"
proof_prompt="${SYNTH_TUI_PROMPT:-Report the active Synth budget in one concise sentence.}"

for required_command in fd node rg tmux; do
	if ! command -v "${required_command}" >/dev/null 2>&1; then
		printf 'proof-faux-tui: %s is required\n' "${required_command}" >&2
		exit 1
	fi
done
if [[ ! -f "${cli_path}" ]]; then
	printf 'proof-faux-tui: built CLI missing at %s; run npm run build first\n' "${cli_path}" >&2
	exit 1
fi
if [[ ! -f "${fixture_path}" ]]; then
	printf 'proof-faux-tui: faux extension fixture missing at %s\n' "${fixture_path}" >&2
	exit 1
fi

proof_dir="$(mktemp -d /tmp/synth-proof-faux-tui.XXXXXX)"
proof_agent_dir="${proof_dir}/agent"
proof_socket="${proof_dir}/daemon.sock"
proof_session="synth-proof-faux-tui-$$-${RANDOM}"
proof_session_owned=0
proof_shutdown_complete=0
mkdir -p "${proof_agent_dir}" "${proof_dir}/xdg-cache" "${proof_dir}/xdg-config" "${proof_dir}/xdg-data"

proof_environment=(
	"PATH=${PATH}"
	"HOME=${HOME}"
	"SHELL=${SHELL:-/bin/sh}"
	"TERM=${TERM:-xterm-256color}"
	"COLORTERM=truecolor"
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
	if [[ "${proof_session_owned}" -eq 1 ]] && tmux has-session -t "=${proof_session}" 2>/dev/null; then
		tmux kill-session -t "=${proof_session}" >/dev/null 2>&1
	fi
	if [[ "${proof_shutdown_complete}" -eq 0 ]]; then
		if ! shutdown_private_daemon >/dev/null 2>&1 || [[ -e "${proof_socket}" ]]; then
			printf 'proof-faux-tui: could not prove private daemon shutdown; preserving %s\n' "${proof_dir}" >&2
			proof_exit_code=1
			proof_preserve_dir=1
		fi
	fi
	if [[ "${proof_exit_code}" -ne 0 ]]; then
		for log_file in "${proof_agent_dir}"/logs/*.log; do
			[[ -f "${log_file}" ]] || continue
			printf 'proof-faux-tui: daemon log %s\n' "${log_file}" >&2
			sed -n '1,240p' "${log_file}" >&2
		done
	fi
	if [[ "${proof_preserve_dir}" -eq 0 ]]; then
		case "${proof_dir}" in
			/tmp/synth-proof-faux-tui.*) rm -rf -- "${proof_dir}" ;;
			*)
				printf 'proof-faux-tui: refusing to remove unexpected path %s\n' "${proof_dir}" >&2
				proof_exit_code=1
				;;
		esac
	fi
	exit "${proof_exit_code}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

/usr/bin/env -i "${proof_environment[@]}" node --input-type=module --eval '
const allowed = new Set([
  "PATH", "HOME", "SHELL", "TERM", "COLORTERM", "LANG", "LC_ALL", "LC_CTYPE",
  "PRIME_AGENT_CODING_AGENT_DIR", "PRIME_AGENT_INTERNAL_DAEMON_SUPERVISOR_REGISTRY_DIR",
  "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PI_OFFLINE",
  "PI_SKIP_VERSION_CHECK", "DO_NOT_TRACK", "PRIME_AGENT_TELEMETRY", "HTTP_PROXY",
  "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy",
  "__CF_USER_TEXT_ENCODING",
]);
const unexpected = Object.keys(process.env).filter((name) => !allowed.has(name)).sort();
if (unexpected.length > 0) throw new Error(`unexpected environment names: ${unexpected.join(", ")}`);
'

printf -v proof_command '%q ' \
	/usr/bin/env -i \
	"${proof_environment[@]}" \
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
	--no-session
proof_command+='; proof_tui_exit_code=$?; printf '\''\n%s:%s\n'\'' '\''__SYNTH_TUI_EXIT__'\'' "${proof_tui_exit_code}"'

proof_shell_ready="__SYNTH_PROOF_SHELL_READY__"
proof_pane="$(tmux new-session -d -P -F '#{pane_id}' -s "${proof_session}" -c "${repo_root}" -x 80 -y 48 /usr/bin/env "PS1=${proof_shell_ready} " /bin/bash --noprofile --norc)"
proof_session_owned=1
proof_shell_started=0
for ((proof_attempt = 0; proof_attempt < 40; proof_attempt++)); do
	proof_capture="$(tmux capture-pane -t "${proof_pane}" -p -S -)"
	if grep -Fq "${proof_shell_ready}" <<<"${proof_capture}"; then
		proof_shell_started=1
		break
	fi
	sleep 0.05
done
if [[ "${proof_shell_started}" -ne 1 ]]; then
	printf '%s\n' "${proof_capture}" >&2
	printf 'proof-faux-tui: private shell did not become ready\n' >&2
	exit 1
fi
tmux send-keys -t "${proof_pane}" -l "${proof_command}"
tmux send-keys -t "${proof_pane}" Enter

proof_capture=""
proof_ready=0
for ((proof_attempt = 0; proof_attempt < 240; proof_attempt++)); do
	if ! tmux has-session -t "=${proof_session}" 2>/dev/null; then
		break
	fi
	proof_capture="$(tmux capture-pane -t "${proof_pane}" -p -S -)"
	if grep -Eq 'version[[:space:]]+v[0-9]+' <<<"${proof_capture}" && \
		grep -Eq 'model[[:space:]]+faux' <<<"${proof_capture}" && \
		grep -Eq 'cwd[[:space:]]+.*synth' <<<"${proof_capture}"; then
		proof_ready=1
		break
	fi
	sleep 0.25
done

printf '%s\n' "${proof_capture}"
if [[ "${proof_ready}" -ne 1 ]]; then
	printf 'proof-faux-tui: TUI did not reach the ready screen\n' >&2
	exit 1
fi
if grep -Eiq 'Login to|No models available|No API key found|unknown model|provider error|extension.*error|Could not start the background service|models\.json error|Error:' <<<"${proof_capture}"; then
	printf 'proof-faux-tui: TUI displayed a startup error\n' >&2
	exit 1
fi
if [[ ! -S "${proof_socket}" ]]; then
	printf 'proof-faux-tui: private daemon socket was not observed\n' >&2
	exit 1
fi

proof_evidence_dir="${evidence_root}/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "${proof_evidence_dir}"
tmux capture-pane -t "${proof_pane}" -p >"${proof_evidence_dir}/boot.txt"
tmux capture-pane -t "${proof_pane}" -p -e >"${proof_evidence_dir}/boot.ansi.txt"
if ! grep -Fq 'Prime METH' "${proof_evidence_dir}/boot.txt" || \
	! grep -Fq 'prime METH>' "${proof_evidence_dir}/boot.txt"; then
	printf 'proof-faux-tui: branded boot or prompt missing from capture\n' >&2
	exit 1
fi

# Any key skips the boot animation and is consumed. Escape is also harmless if
# the animation completed between readiness detection and this input.
tmux send-keys -t "${proof_pane}" Escape
sleep 0.2
tmux send-keys -t "${proof_pane}" -l '/synth budget'
tmux send-keys -t "${proof_pane}" Enter
proof_budget_ready=0
for ((proof_attempt = 0; proof_attempt < 80; proof_attempt++)); do
	proof_capture="$(tmux capture-pane -t "${proof_pane}" -p -S -)"
	if grep -Fq '<budget>' <<<"${proof_capture}"; then
		proof_budget_ready=1
		break
	fi
	sleep 0.25
done
if [[ "${proof_budget_ready}" -ne 1 ]]; then
	printf '%s\n' "${proof_capture}" >&2
	printf 'proof-faux-tui: /synth budget did not render the budget block\n' >&2
	exit 1
fi
tmux capture-pane -t "${proof_pane}" -p -S - >"${proof_evidence_dir}/budget.txt"
tmux capture-pane -t "${proof_pane}" -p -e -S - >"${proof_evidence_dir}/budget.ansi.txt"
tmux send-keys -t "${proof_pane}" -l "${proof_prompt}"
tmux send-keys -t "${proof_pane}" Enter
proof_turn_ready=0
for ((proof_attempt = 0; proof_attempt < 240; proof_attempt++)); do
	proof_capture="$(tmux capture-pane -t "${proof_pane}" -p -S -)"
	if grep -Fq 'upgrade response 1' <<<"${proof_capture}" && \
		grep -Eq 'prefill[[:space:]]+[0-9,.]+[[:space:]]+tok/s.*decode[[:space:]]+[0-9,.]+[[:space:]]+tok/s' <<<"${proof_capture}"; then
		proof_turn_ready=1
		break
	fi
	sleep 0.25
done
if [[ "${proof_turn_ready}" -ne 1 ]]; then
	printf '%s\n' "${proof_capture}" >&2
	printf 'proof-faux-tui: faux turn did not populate both throughput rates\n' >&2
	exit 1
fi
proof_prompt_marker="${proof_prompt:0:32}"
if ! grep -Fq "${proof_prompt_marker}" <<<"${proof_capture}" || \
	! grep -Fq '<budget>' "${proof_evidence_dir}/budget.txt" || \
	! grep -Fq '</budget>' "${proof_evidence_dir}/budget.txt"; then
	printf '%s\n' "${proof_capture}" >&2
	printf 'proof-faux-tui: task prompt or visible budget block missing from capture\n' >&2
	exit 1
fi
tmux capture-pane -t "${proof_pane}" -p >"${proof_evidence_dir}/rail.txt"
tmux capture-pane -t "${proof_pane}" -p -e >"${proof_evidence_dir}/rail.ansi.txt"
printf '%s\n' "${proof_capture}"
printf 'proof-faux-tui: evidence staged at %s\n' "${proof_evidence_dir}"

tmux send-keys -t "${proof_pane}" -l '/quit'
tmux send-keys -t "${proof_pane}" Enter
proof_tui_exited=0
for ((proof_attempt = 0; proof_attempt < 80; proof_attempt++)); do
	proof_capture="$(tmux capture-pane -t "${proof_pane}" -p -S -)"
	if grep -Fxq '__SYNTH_TUI_EXIT__:0' <<<"${proof_capture}"; then
		proof_tui_exited=1
		break
	fi
	if grep -Eq '^__SYNTH_TUI_EXIT__:[1-9][0-9]*$' <<<"${proof_capture}"; then
		break
	fi
	sleep 0.25
done
if [[ "${proof_tui_exited}" -ne 1 ]]; then
	printf '%s\n' "${proof_capture}" >&2
	printf 'proof-faux-tui: TUI did not exit cleanly through /quit\n' >&2
	exit 1
fi

if ! shutdown_private_daemon; then
	printf 'proof-faux-tui: private daemon shutdown failed\n' >&2
	exit 1
fi
if [[ -e "${proof_socket}" ]]; then
	printf 'proof-faux-tui: private daemon socket remains after shutdown\n' >&2
	exit 1
fi
proof_shutdown_complete=1

tmux kill-session -t "=${proof_session}"
proof_session_owned=0

printf 'status=PASS\nbundle=%s\ndaemon_socket=removed\n' "${cli_path}" >"${proof_evidence_dir}/SUCCESS"
if [[ ! -s "${proof_evidence_dir}/SUCCESS" ]]; then
	printf 'proof-faux-tui: success receipt was not persisted\n' >&2
	exit 1
fi
printf 'proof-faux-tui: evidence %s (SUCCESS receipt present)\n' "${proof_evidence_dir}"
printf 'proof-faux-tui: PASS\n'
