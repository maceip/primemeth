#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
synth_root="$(cd "${script_dir}/.." && pwd)"
catalog_path="${synth_root}/packages/ai/src/models.generated.ts"
ai_manifest_path="${synth_root}/packages/ai/package.json"
cli_path="${synth_root}/packages/coding-agent/dist/cli.js"
faux_fixture_path="${synth_root}/packages/coding-agent/test/fixtures/eng-4600-faux-extension.ts"
setup_script="${script_dir}/setup-offline.sh"

for required_command in git node npm perl tmux; do
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

for required_file in "${cli_path}" "${faux_fixture_path}" "${setup_script}"; do
	if [[ ! -f "${required_file}" ]]; then
		printf 'synth-demo: required A1 artifact missing at %s\n' "${required_file}" >&2
		exit 1
	fi
done

demo_dir="$(mktemp -d /tmp/synth-demo-a1.XXXXXX)"
demo_agent_dir="${demo_dir}/agent"
demo_kernel_venv="${demo_dir}/kernel-venv"
demo_tui_socket="${demo_dir}/tui.sock"
demo_print_socket="${demo_dir}/print.sock"
demo_tui_session="synth-demo-a1-$$-${RANDOM}"
demo_tui_owned=0
demo_tui_started=0
demo_tui_shutdown=0
demo_print_started=0
demo_print_shutdown=0
mkdir -p "${demo_agent_dir}"

demo_environment=(
	"PRIME_AGENT_CODING_AGENT_DIR=${demo_agent_dir}"
	"PRIME_AGENT_KERNEL_VENV=${demo_kernel_venv}"
	"TMPDIR=${demo_dir}"
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

shutdown_demo_daemon() {
	local socket_path=$1
	(
		cd "${synth_root}"
		/usr/bin/env "${demo_environment[@]}" node --input-type=module --eval '
import { shutdownDaemonAndWait } from "./packages/coding-agent/dist/cli/daemon-launch.js";

if (!(await shutdownDaemonAndWait(process.argv[1], 10_000))) {
	process.exit(1);
}
' "${socket_path}"
	)
}

cleanup_demo() {
	local demo_exit_code=$?
	local preserve_demo_dir=0
	trap - EXIT
	set +e
	if [[ "${demo_tui_owned}" -eq 1 ]] && tmux has-session -t "=${demo_tui_session}" 2>/dev/null; then
		tmux kill-session -t "=${demo_tui_session}" >/dev/null 2>&1
	fi
	if [[ "${demo_tui_started}" -eq 1 && "${demo_tui_shutdown}" -eq 0 ]]; then
		if ! shutdown_demo_daemon "${demo_tui_socket}" >/dev/null 2>&1 || [[ -e "${demo_tui_socket}" ]]; then
			printf 'synth-demo: could not prove TUI daemon shutdown; preserving %s\n' "${demo_dir}" >&2
			demo_exit_code=1
			preserve_demo_dir=1
		fi
	fi
	if [[ "${demo_print_started}" -eq 1 && "${demo_print_shutdown}" -eq 0 ]]; then
		if ! shutdown_demo_daemon "${demo_print_socket}" >/dev/null 2>&1 || [[ -e "${demo_print_socket}" ]]; then
			printf 'synth-demo: could not prove print daemon shutdown; preserving %s\n' "${demo_dir}" >&2
			demo_exit_code=1
			preserve_demo_dir=1
		fi
	fi
	if [[ "${demo_exit_code}" -ne 0 ]]; then
		for log_file in "${demo_agent_dir}"/logs/*.log; do
			[[ -f "${log_file}" ]] || continue
			printf 'synth-demo: daemon log %s\n' "${log_file}" >&2
			sed -n '1,240p' "${log_file}" >&2
		done
	fi
	if [[ "${preserve_demo_dir}" -eq 0 ]]; then
		case "${demo_dir}" in
			/tmp/synth-demo-a1.*) rm -rf -- "${demo_dir}" ;;
			*)
				printf 'synth-demo: refusing to remove unexpected path %s\n' "${demo_dir}" >&2
				demo_exit_code=1
				;;
		esac
	fi
	exit "${demo_exit_code}"
}

trap cleanup_demo EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

catalog_receipt() {
	node --input-type=module --eval '
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";

const data = readFileSync(process.argv[1]);
const stat = statSync(process.argv[1], { bigint: true });
console.log(JSON.stringify({
	hash: createHash("sha256").update(data).digest("hex"),
	ino: String(stat.ino),
	mtimeNs: String(stat.mtimeNs),
}));
' "${demo_agent_dir}/models.json"
}

first_setup_output="$(
	cd "${synth_root}"
	PRIME_AGENT_CODING_AGENT_DIR="${demo_agent_dir}" \
		PRIME_AGENT_KERNEL_VENV="${demo_kernel_venv}" \
		"${setup_script}"
)"
printf '%s\n' "${first_setup_output}"
if ! grep -Fxq 'models.json: added provider local' <<<"${first_setup_output}"; then
	printf 'synth-demo: first setup did not install the local provider\n' >&2
	exit 1
fi
catalog_before="$(catalog_receipt)"

second_setup_output="$(
	cd "${synth_root}"
	PRIME_AGENT_CODING_AGENT_DIR="${demo_agent_dir}" \
		PRIME_AGENT_KERNEL_VENV="${demo_kernel_venv}" \
		"${setup_script}"
)"
printf '%s\n' "${second_setup_output}"
if ! grep -Fxq 'models.json: no changes' <<<"${second_setup_output}"; then
	printf 'synth-demo: second setup did not report a semantic no-op\n' >&2
	exit 1
fi
catalog_after="$(catalog_receipt)"
if [[ "${catalog_before}" != "${catalog_after}" ]]; then
	printf 'synth-demo: second setup changed catalog bytes, inode, or mtime\n' >&2
	exit 1
fi

node --input-type=module --eval '
import { readFileSync } from "node:fs";

const catalog = JSON.parse(readFileSync(process.argv[1], "utf8"));
const local = catalog.providers?.local;
if (local?.baseUrl !== "http://localhost:8000/v1" || local?.api !== "openai-completions") {
	throw new Error("installed local provider does not use the shipped localhost endpoint");
}
const ids = new Set((local.models ?? []).map((model) => model.id));
if (!ids.has("local-main") || !ids.has("local-small")) {
	throw new Error("installed local provider is missing shipped model IDs");
}
' "${demo_agent_dir}/models.json"
printf 'synth-demo: Phase 1 A1 setup idempotence PASS (%s)\n' "${catalog_after}"

existing_tmux_sessions="$(
	{
		tmux list-sessions -F '#{session_name}' 2>/dev/null || true
	} | wc -l | tr -d ' '
)"
printf 'synth-demo: pre-run process receipt tmux_sessions=%s tui_socket=absent print_socket=absent\n' \
	"${existing_tmux_sessions}"

printf -v demo_tui_command '%q ' \
	/usr/bin/env \
	"${demo_environment[@]}" \
	node "${cli_path}" \
	--offline \
	--cwd "${synth_root}" \
	--daemon-socket "${demo_tui_socket}" \
	--no-tools \
	--no-skills \
	--no-prompt-templates \
	--no-themes \
	--no-context-files \
	--no-session
demo_tui_command+='; demo_tui_exit_code=$?; printf '\''\n%s:%s\n'\'' '\''__SYNTH_DEMO_TUI_EXIT__'\'' "${demo_tui_exit_code}"'

demo_tui_pane="$(tmux new-session -d -P -F '#{pane_id}' -s "${demo_tui_session}" -c "${synth_root}" -x 80 -y 24 /bin/bash --noprofile --norc)"
demo_tui_owned=1
demo_tui_started=1
tmux send-keys -t "${demo_tui_pane}" -l "${demo_tui_command}"
tmux send-keys -t "${demo_tui_pane}" Enter

demo_tui_capture=""
demo_tui_ready=0
for ((demo_attempt = 0; demo_attempt < 240; demo_attempt++)); do
	if ! tmux has-session -t "=${demo_tui_session}" 2>/dev/null; then
		break
	fi
	demo_tui_capture="$(tmux capture-pane -t "${demo_tui_pane}" -p -S -)"
	if grep -Eq 'version[[:space:]]+v[0-9]+' <<<"${demo_tui_capture}" && \
		grep -Eq 'model[[:space:]]+local-main' <<<"${demo_tui_capture}" && \
		grep -Eq 'cwd[[:space:]]+.*synth' <<<"${demo_tui_capture}"; then
		demo_tui_ready=1
		break
	fi
	sleep 0.25
done
printf '%s\n' "${demo_tui_capture}"
if [[ "${demo_tui_ready}" -ne 1 ]]; then
	printf 'synth-demo: local-default TUI did not reach the ready screen\n' >&2
	exit 1
fi
if grep -Eiq 'Login to|No models available|No API key found|unknown model|provider error|Could not start the background service|models\.json error|Error:' <<<"${demo_tui_capture}"; then
	printf 'synth-demo: local-default TUI displayed a startup or login error\n' >&2
	exit 1
fi
if [[ ! -S "${demo_tui_socket}" ]]; then
	printf 'synth-demo: local-default TUI daemon socket was not observed\n' >&2
	exit 1
fi

tmux send-keys -t "${demo_tui_pane}" -l '/quit'
tmux send-keys -t "${demo_tui_pane}" Enter
demo_tui_exited=0
for ((demo_attempt = 0; demo_attempt < 80; demo_attempt++)); do
	demo_tui_capture="$(tmux capture-pane -t "${demo_tui_pane}" -p -S -)"
	if grep -Fxq '__SYNTH_DEMO_TUI_EXIT__:0' <<<"${demo_tui_capture}"; then
		demo_tui_exited=1
		break
	fi
	if grep -Eq '^__SYNTH_DEMO_TUI_EXIT__:[1-9][0-9]*$' <<<"${demo_tui_capture}"; then
		break
	fi
	sleep 0.25
done
if [[ "${demo_tui_exited}" -ne 1 ]]; then
	printf '%s\n' "${demo_tui_capture}" >&2
	printf 'synth-demo: local-default TUI did not exit cleanly through /quit\n' >&2
	exit 1
fi
if ! shutdown_demo_daemon "${demo_tui_socket}" || [[ -e "${demo_tui_socket}" ]]; then
	printf 'synth-demo: local-default TUI daemon shutdown failed\n' >&2
	exit 1
fi
demo_tui_shutdown=1
tmux kill-session -t "=${demo_tui_session}"
demo_tui_owned=0
printf 'synth-demo: Phase 1 A1 local-default TUI PASS\n'

demo_print_stdout="${demo_dir}/print.stdout"
demo_print_stderr="${demo_dir}/print.stderr"
demo_print_started=1
demo_print_exit=0
(
	cd "${synth_root}"
	/usr/bin/perl -e 'alarm shift @ARGV; exec @ARGV or die "exec: $!\n"' \
		60 \
		/usr/bin/env "${demo_environment[@]}" \
		node "${cli_path}" \
			--offline \
			--cwd "${synth_root}" \
			--daemon-socket "${demo_print_socket}" \
			--model faux/faux \
			--extension "${faux_fixture_path}" \
			--no-tools \
			--no-skills \
			--no-prompt-templates \
			--no-themes \
			--no-context-files \
			--no-session \
			--print "Synth A1 faux print demo"
) >"${demo_print_stdout}" 2>"${demo_print_stderr}" || demo_print_exit=$?
if [[ "${demo_print_exit}" -ne 0 ]]; then
	sed -n '1,240p' "${demo_print_stdout}" >&2
	sed -n '1,240p' "${demo_print_stderr}" >&2
	printf 'synth-demo: configured faux print exited %s\n' "${demo_print_exit}" >&2
	exit 1
fi
if [[ -s "${demo_print_stderr}" ]]; then
	sed -n '1,240p' "${demo_print_stderr}" >&2
	printf 'synth-demo: configured faux print emitted stderr\n' >&2
	exit 1
fi
if ! diff -u <(printf 'upgrade response 1\n') "${demo_print_stdout}"; then
	printf 'synth-demo: configured faux print output mismatch\n' >&2
	exit 1
fi
if [[ ! -S "${demo_print_socket}" ]]; then
	printf 'synth-demo: configured faux print daemon socket was not observed\n' >&2
	exit 1
fi
if ! shutdown_demo_daemon "${demo_print_socket}" || [[ -e "${demo_print_socket}" ]]; then
	printf 'synth-demo: configured faux print daemon shutdown failed\n' >&2
	exit 1
fi
demo_print_shutdown=1
printf 'synth-demo: Phase 1 A1 configured faux print PASS\n'
