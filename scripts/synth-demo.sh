#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
synth_root="$(cd "${script_dir}/.." && pwd)"
catalog_path="${synth_root}/packages/ai/src/models.generated.ts"
ai_manifest_path="${synth_root}/packages/ai/package.json"
cli_path="${synth_root}/packages/coding-agent/dist/cli.js"
faux_fixture_path="${synth_root}/packages/coding-agent/test/fixtures/eng-4600-faux-extension.ts"
summary_fixture_path="${synth_root}/packages/coding-agent/test/fixtures/synth-local-summary-extension.ts"
setup_script="${script_dir}/setup-offline.sh"
tsx_path="${synth_root}/node_modules/.bin/tsx"
phase2_driver_path="${script_dir}/synth-phase2-demo.ts"
phase3_driver_path="${script_dir}/synth-phase3-demo.ts"
phase4_driver_path="${script_dir}/synth-phase4-demo.ts"
phase5_driver_path="${script_dir}/synth-phase5-demo.ts"
phase7_driver_path="${script_dir}/synth-phase7-demo.ts"
phase8_compiler_crash_driver_path="${script_dir}/synth-phase8-compiler-crash.ts"
phase8_endpoint_driver_path="${script_dir}/synth-phase8-endpoint-down.ts"
phase8_failure_tests_path="${synth_root}/tests/harness_regression/test_phase8_failures.py"
optimizer_config_path="${synth_root}/optimizer/configs/synth_local.yaml"
config_doc_generator_path="${script_dir}/generate-synth-config-doc.ts"
wiring_script="${script_dir}/check-wiring.sh"

for required_command in git node npm perl tmux uv; do
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
if [[ -n "$(git -C "${synth_root}" status --porcelain=v1 --untracked-files=all)" ]]; then
	printf 'synth-demo: Phase 6 requires a clean worktree for its gated optimizer commit\n' >&2
	exit 1
fi

source_branch="$(git -C "${synth_root}" branch --show-current)"
source_head="$(git -C "${synth_root}" rev-parse HEAD)"

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

for required_file in \
	"${cli_path}" \
	"${faux_fixture_path}" \
	"${summary_fixture_path}" \
	"${setup_script}" \
	"${tsx_path}" \
	"${phase2_driver_path}" \
	"${phase3_driver_path}" \
	"${phase4_driver_path}" \
	"${phase5_driver_path}" \
	"${phase7_driver_path}" \
	"${phase8_compiler_crash_driver_path}" \
	"${phase8_endpoint_driver_path}" \
	"${phase8_failure_tests_path}" \
	"${optimizer_config_path}" \
	"${config_doc_generator_path}" \
	"${wiring_script}"; do
	if [[ ! -f "${required_file}" ]]; then
		printf 'synth-demo: required A1 artifact missing at %s\n' "${required_file}" >&2
		exit 1
	fi
done

demo_dir="$(mktemp -d /tmp/synth-demo-phase1.XXXXXX)"
demo_agent_dir="${demo_dir}/agent"
demo_kernel_venv="${demo_dir}/kernel-venv"
demo_tui_socket="${demo_dir}/tui.sock"
demo_print_socket="${demo_dir}/print.sock"
demo_summary_socket="${demo_dir}/summary.sock"
demo_disabled_summary_socket="${demo_dir}/disabled-summary.sock"
demo_summary_session_dir="${demo_dir}/summary-sessions"
demo_disabled_summary_session_dir="${demo_dir}/disabled-summary-sessions"
demo_disabled_summary_project="${demo_dir}/no-summary-project"
demo_broken_skill_project="${demo_dir}/broken-skill-project"
demo_tui_session="synth-demo-a1-$$-${RANDOM}"
demo_summary_tui_session="synth-demo-a2-summary-$$-${RANDOM}"
demo_disabled_summary_tui_session="synth-demo-a2-disabled-$$-${RANDOM}"
demo_tui_owned=0
demo_summary_tui_owned=0
demo_disabled_summary_tui_owned=0
demo_tui_started=0
demo_tui_shutdown=0
demo_print_started=0
demo_print_shutdown=0
demo_summary_started=0
demo_summary_shutdown=0
demo_disabled_summary_started=0
demo_disabled_summary_shutdown=0
mkdir -p "${demo_agent_dir}" "${demo_disabled_summary_project}"

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
	if [[ "${demo_summary_tui_owned}" -eq 1 ]] && tmux has-session -t "=${demo_summary_tui_session}" 2>/dev/null; then
		tmux kill-session -t "=${demo_summary_tui_session}" >/dev/null 2>&1
	fi
	if [[ "${demo_disabled_summary_tui_owned}" -eq 1 ]] && tmux has-session -t "=${demo_disabled_summary_tui_session}" 2>/dev/null; then
		tmux kill-session -t "=${demo_disabled_summary_tui_session}" >/dev/null 2>&1
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
	if [[ "${demo_summary_started}" -eq 1 && "${demo_summary_shutdown}" -eq 0 ]]; then
		if ! shutdown_demo_daemon "${demo_summary_socket}" >/dev/null 2>&1 || [[ -e "${demo_summary_socket}" ]]; then
			printf 'synth-demo: could not prove summary daemon shutdown; preserving %s\n' "${demo_dir}" >&2
			demo_exit_code=1
			preserve_demo_dir=1
		fi
	fi
	if [[ "${demo_disabled_summary_started}" -eq 1 && "${demo_disabled_summary_shutdown}" -eq 0 ]]; then
		if ! shutdown_demo_daemon "${demo_disabled_summary_socket}" >/dev/null 2>&1 || [[ -e "${demo_disabled_summary_socket}" ]]; then
			printf 'synth-demo: could not prove disabled-summary daemon shutdown; preserving %s\n' "${demo_dir}" >&2
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
			/tmp/synth-demo-phase1.*) rm -rf -- "${demo_dir}" ;;
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

session_has_exact_summary() {
	node --input-type=module --eval '
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const [sessionDir, expectedSummary] = process.argv.slice(1);
if (!existsSync(sessionDir)) process.exit(1);
for (const name of readdirSync(sessionDir)) {
	if (!name.endsWith(".jsonl")) continue;
	for (const line of readFileSync(join(sessionDir, name), "utf8").split("\n")) {
		if (!line.trim()) continue;
		let entry;
		try {
			entry = JSON.parse(line);
		} catch {
			continue;
		}
		if (
			entry.type === "agent_status" &&
			entry.status?.summary === expectedSummary &&
			entry.status?.taskState === "completed"
		) {
			process.exit(0);
		}
	}
}
process.exit(1);
' "$1" "$2"
}

session_has_assistant_without_nonempty_summary() {
	node --input-type=module --eval '
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const sessionDir = process.argv[1];
if (!existsSync(sessionDir)) process.exit(1);
let sawAssistant = false;
for (const name of readdirSync(sessionDir)) {
	if (!name.endsWith(".jsonl")) continue;
	for (const line of readFileSync(join(sessionDir, name), "utf8").split("\n")) {
		if (!line.trim()) continue;
		let entry;
		try {
			entry = JSON.parse(line);
		} catch {
			continue;
		}
		if (entry.type === "message" && entry.message?.role === "assistant") {
			sawAssistant = true;
		}
		if (entry.type === "agent_status" && String(entry.status?.summary ?? "").trim()) {
			process.exit(1);
		}
	}
}
process.exit(sawAssistant ? 0 : 1);
' "$1"
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

node --input-type=module --eval '
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [projectRoot, fixtureRoot] = process.argv.slice(1);
const skillRoot = join(fixtureRoot, ".prime", "agent", "skills", "json");
mkdirSync(join(skillRoot, "src", "json"), { recursive: true });
writeFileSync(
	join(fixtureRoot, ".prime", "agent", "settings.json"),
	readFileSync(join(projectRoot, ".prime", "agent", "settings.json")),
);
writeFileSync(
	join(skillRoot, "SKILL.md"),
	`---
name: json
description: Deterministic standard-library collision failure fixture.
---

# Broken JSON Skill
`,
);
writeFileSync(join(skillRoot, "pyproject.toml"), "this is deliberately invalid TOML = [\n");
writeFileSync(join(skillRoot, "src", "json", "__init__.py"), "VALUE = 1\n");
' "${synth_root}" "${demo_broken_skill_project}"

broken_bootstrap_output="${demo_dir}/broken-bootstrap.log"
broken_bootstrap_exit=0
(
	cd "${synth_root}"
	/usr/bin/env "${demo_environment[@]}" \
		PRIME_AGENT_INSTALL_UV=0 \
		"${tsx_path}" "${script_dir}/bootstrap-synth-kernel.ts" "${demo_broken_skill_project}"
) >"${broken_bootstrap_output}" 2>&1 || broken_bootstrap_exit=$?
if [[ "${broken_bootstrap_exit}" -eq 0 ]]; then
	sed -n '1,240p' "${broken_bootstrap_output}" >&2
	printf 'synth-demo: bootstrap accepted a failed enabled Python skill install\n' >&2
	exit 1
fi
if ! grep -Fq 'Python skill json failed to install' "${broken_bootstrap_output}" || \
	! grep -Fq 'enabled Python skill is absent or stale in the bootstrap receipt' "${broken_bootstrap_output}"; then
	sed -n '1,240p' "${broken_bootstrap_output}" >&2
	printf 'synth-demo: bootstrap failure did not identify the unavailable enabled skill\n' >&2
	exit 1
fi
printf 'synth-demo: Phase 1 A1 partial Python-skill install rejection PASS\n'

existing_tmux_sessions="$(
	{
		tmux list-sessions -F '#{session_name}' 2>/dev/null || true
	} | wc -l | tr -d ' '
)"
printf 'synth-demo: pre-run process receipt tmux_sessions=%s tui_socket=absent print_socket=absent summary_sockets=absent\n' \
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

demo_tui_pane="$(tmux new-session -d -P -F '#{pane_id}' -s "${demo_tui_session}" -c "${synth_root}" -x 80 -y 48 /bin/bash --noprofile --norc)"
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
		grep -Eq 'cwd[[:space:]]+.*synth' <<<"${demo_tui_capture}" && \
		grep -Fq '+-------------------------------------------------------------------+' <<<"${demo_tui_capture}"; then
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

tmux send-keys -t "${demo_tui_pane}" -l '/synth status'
tmux send-keys -t "${demo_tui_pane}" Enter
demo_synth_status=0
for ((demo_attempt = 0; demo_attempt < 80; demo_attempt++)); do
	demo_tui_capture="$(tmux capture-pane -t "${demo_tui_pane}" -p -S -)"
	if grep -Fq 'configEcho=' <<<"${demo_tui_capture}" && \
		grep -Eq 'bridge=up pid=[0-9]+ protocol=1 memory=pending' <<<"${demo_tui_capture}" && \
		grep -Fq 'budgets=[Budget:' <<<"${demo_tui_capture}"; then
		demo_synth_status=1
		break
	fi
	sleep 0.25
done
if [[ "${demo_synth_status}" -ne 1 ]]; then
	printf '%s\n' "${demo_tui_capture}" >&2
	printf 'synth-demo: discovered /synth status did not render bridge and config state\n' >&2
	exit 1
fi
printf 'synth-demo: Phase 2 discovered /synth status PASS\n'

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

printf -v demo_summary_command '%q ' \
	/usr/bin/env \
	"${demo_environment[@]}" \
	node "${cli_path}" \
	--offline \
	--cwd "${synth_root}" \
	--session-dir "${demo_summary_session_dir}" \
	--daemon-socket "${demo_summary_socket}" \
	--model local/local-main \
	--extension "${summary_fixture_path}" \
	--no-tools \
	--no-skills \
	--no-prompt-templates \
	--no-themes \
	--no-context-files
demo_summary_command+='; demo_summary_exit_code=$?; printf '\''\n%s:%s\n'\'' '\''__SYNTH_DEMO_SUMMARY_EXIT__'\'' "${demo_summary_exit_code}"'

demo_summary_pane="$(tmux new-session -d -P -F '#{pane_id}' -s "${demo_summary_tui_session}" -c "${synth_root}" -x 80 -y 24 /bin/bash --noprofile --norc)"
demo_summary_tui_owned=1
demo_summary_started=1
tmux send-keys -t "${demo_summary_pane}" -l "${demo_summary_command}"
tmux send-keys -t "${demo_summary_pane}" Enter

demo_summary_capture=""
demo_summary_ready=0
for ((demo_attempt = 0; demo_attempt < 240; demo_attempt++)); do
	if ! tmux has-session -t "=${demo_summary_tui_session}" 2>/dev/null; then
		break
	fi
	demo_summary_capture="$(tmux capture-pane -t "${demo_summary_pane}" -p -S -)"
	if grep -Eq 'version[[:space:]]+v[0-9]+' <<<"${demo_summary_capture}" && \
		grep -Eq 'model[[:space:]]+local-main' <<<"${demo_summary_capture}"; then
		demo_summary_ready=1
		break
	fi
	sleep 0.25
done
if [[ "${demo_summary_ready}" -ne 1 ]]; then
	printf '%s\n' "${demo_summary_capture}" >&2
	printf 'synth-demo: configured-summary TUI did not reach the ready screen\n' >&2
	exit 1
fi

tmux send-keys -t "${demo_summary_pane}" -l 'Demonstrate the configured local session recap'
tmux send-keys -t "${demo_summary_pane}" Enter
demo_summary_answered=0
for ((demo_attempt = 0; demo_attempt < 240; demo_attempt++)); do
	demo_summary_capture="$(tmux capture-pane -t "${demo_summary_pane}" -p -S -)"
	if grep -Fq 'Synth A2 primary response' <<<"${demo_summary_capture}"; then
		demo_summary_answered=1
		break
	fi
	sleep 0.25
done
if [[ "${demo_summary_answered}" -ne 1 ]]; then
	printf '%s\n' "${demo_summary_capture}" >&2
	printf 'synth-demo: configured-summary TUI did not receive the faux primary response\n' >&2
	exit 1
fi

demo_summary_observed=0
for ((demo_attempt = 0; demo_attempt < 80; demo_attempt++)); do
	if session_has_exact_summary \
		"${demo_summary_session_dir}" \
		'Completed the Synth local recap demonstration'; then
		demo_summary_observed=1
		break
	fi
	sleep 0.25
done
if [[ "${demo_summary_observed}" -ne 1 ]]; then
	printf 'synth-demo: configured local recap was not persisted as agent_status\n' >&2
	exit 1
fi
if ! shutdown_demo_daemon "${demo_summary_socket}" || [[ -e "${demo_summary_socket}" ]]; then
	printf 'synth-demo: configured-summary daemon shutdown failed\n' >&2
	exit 1
fi
demo_summary_shutdown=1
if tmux has-session -t "=${demo_summary_tui_session}" 2>/dev/null; then
	tmux kill-session -t "=${demo_summary_tui_session}"
fi
demo_summary_tui_owned=0
printf 'synth-demo: Phase 1 A2 configured local recap PASS\n'

printf -v demo_disabled_summary_command '%q ' \
	/usr/bin/env \
	"${demo_environment[@]}" \
	node "${cli_path}" \
	--offline \
	--cwd "${demo_disabled_summary_project}" \
	--session-dir "${demo_disabled_summary_session_dir}" \
	--daemon-socket "${demo_disabled_summary_socket}" \
	--model local/local-main \
	--extension "${summary_fixture_path}" \
	--no-tools \
	--no-skills \
	--no-prompt-templates \
	--no-themes \
	--no-context-files
demo_disabled_summary_command+='; demo_disabled_summary_exit_code=$?; printf '\''\n%s:%s\n'\'' '\''__SYNTH_DEMO_DISABLED_SUMMARY_EXIT__'\'' "${demo_disabled_summary_exit_code}"'

demo_disabled_summary_pane="$(tmux new-session -d -P -F '#{pane_id}' -s "${demo_disabled_summary_tui_session}" -c "${synth_root}" -x 80 -y 24 /bin/bash --noprofile --norc)"
demo_disabled_summary_tui_owned=1
demo_disabled_summary_started=1
tmux send-keys -t "${demo_disabled_summary_pane}" -l "${demo_disabled_summary_command}"
tmux send-keys -t "${demo_disabled_summary_pane}" Enter

demo_disabled_summary_capture=""
demo_disabled_summary_ready=0
for ((demo_attempt = 0; demo_attempt < 240; demo_attempt++)); do
	if ! tmux has-session -t "=${demo_disabled_summary_tui_session}" 2>/dev/null; then
		break
	fi
	demo_disabled_summary_capture="$(tmux capture-pane -t "${demo_disabled_summary_pane}" -p -S -)"
	if grep -Eq 'version[[:space:]]+v[0-9]+' <<<"${demo_disabled_summary_capture}" && \
		grep -Eq 'model[[:space:]]+local-main' <<<"${demo_disabled_summary_capture}"; then
		demo_disabled_summary_ready=1
		break
	fi
	sleep 0.25
done
if [[ "${demo_disabled_summary_ready}" -ne 1 ]]; then
	printf '%s\n' "${demo_disabled_summary_capture}" >&2
	printf 'synth-demo: disabled-summary TUI did not reach the ready screen\n' >&2
	exit 1
fi

tmux send-keys -t "${demo_disabled_summary_pane}" -l 'Demonstrate missing session recap configuration'
tmux send-keys -t "${demo_disabled_summary_pane}" Enter
demo_disabled_summary_answered=0
for ((demo_attempt = 0; demo_attempt < 240; demo_attempt++)); do
	demo_disabled_summary_capture="$(tmux capture-pane -t "${demo_disabled_summary_pane}" -p -S -)"
	if grep -Fq 'Synth A2 primary response' <<<"${demo_disabled_summary_capture}"; then
		demo_disabled_summary_answered=1
		break
	fi
	sleep 0.25
done
if [[ "${demo_disabled_summary_answered}" -ne 1 ]]; then
	printf '%s\n' "${demo_disabled_summary_capture}" >&2
	printf 'synth-demo: disabled-summary TUI did not receive the faux primary response\n' >&2
	exit 1
fi

sleep 3
if ! session_has_assistant_without_nonempty_summary "${demo_disabled_summary_session_dir}"; then
	printf 'synth-demo: absent recap configuration produced a nonempty summary or no persisted assistant\n' >&2
	exit 1
fi
if ! shutdown_demo_daemon "${demo_disabled_summary_socket}" || [[ -e "${demo_disabled_summary_socket}" ]]; then
	printf 'synth-demo: disabled-summary daemon shutdown failed\n' >&2
	exit 1
fi
demo_disabled_summary_shutdown=1
if tmux has-session -t "=${demo_disabled_summary_tui_session}" 2>/dev/null; then
	tmux kill-session -t "=${demo_disabled_summary_tui_session}"
fi
demo_disabled_summary_tui_owned=0
printf 'synth-demo: Phase 1 A2 missing-config generated-recap suppression PASS\n'

(
	cd "${synth_root}"
	/usr/bin/env \
		-u PI_OFFLINE \
		-u DO_NOT_TRACK \
		-u PRIME_AGENT_TELEMETRY \
		NODE_ENV=production \
		node --input-type=module --eval '
import { existsSync } from "node:fs";
import { join } from "node:path";
import { SettingsManager } from "./packages/coding-agent/dist/core/settings-manager.js";
import { captureAgentCommandUsed, isTelemetryEnabled } from "./packages/coding-agent/dist/core/telemetry.js";

const [projectRoot, agentDir] = process.argv.slice(1);
const telemetryPath = join(agentDir, "telemetry.json");
let fetchCalls = 0;
globalThis.fetch = async () => {
	fetchCalls++;
	throw new Error("telemetry demo forbids network requests");
};

const shippedSettings = SettingsManager.create(projectRoot, agentDir);
if (isTelemetryEnabled(shippedSettings)) {
	throw new Error("shipped Synth settings unexpectedly enable product analytics");
}
await captureAgentCommandUsed({
	agentDir,
	settingsManager: shippedSettings,
	commandName: "model",
});
if (existsSync(telemetryPath)) {
	throw new Error("disabled product analytics created telemetry.json");
}

const events = [];
let flushCount = 0;
const sink = {
	capture(name, properties) {
		events.push({ name, properties });
	},
	async flush() {
		flushCount++;
	},
};
const enabledSettings = SettingsManager.inMemory({ telemetry: { enabled: true } });
if (!isTelemetryEnabled(enabledSettings)) {
	throw new Error("explicit settings opt-in did not enable product analytics");
}
await captureAgentCommandUsed({
	agentDir,
	settingsManager: enabledSettings,
	commandName: "model",
	sink,
});
if (
	events.length !== 1 ||
	events[0]?.name !== "agent command used" ||
	events[0]?.properties?.command_name !== "model" ||
	flushCount !== 1
) {
	throw new Error("explicit settings opt-in did not reach the injected telemetry sink");
}

process.env.PRIME_AGENT_TELEMETRY = "1";
if (!isTelemetryEnabled(SettingsManager.inMemory())) {
	throw new Error("explicit environment opt-in did not enable product analytics");
}
process.env.PI_OFFLINE = "1";
if (isTelemetryEnabled(enabledSettings)) {
	throw new Error("PI_OFFLINE did not override explicit analytics opt-in");
}
if (fetchCalls !== 0 || existsSync(telemetryPath)) {
	throw new Error("telemetry demo used the network or wrote product telemetry state");
}
' "${synth_root}" "${demo_agent_dir}"
)
printf 'synth-demo: Phase 1 A4 analytics opt-in policy PASS\n'

(
	cd "${synth_root}"
	/usr/bin/env "${demo_environment[@]}" node --input-type=module --eval '
import { existsSync } from "node:fs";
import { join } from "node:path";
import { createAgentSessionServices } from "./packages/coding-agent/dist/core/agent-session-services.js";
import { SettingsManager } from "./packages/coding-agent/dist/core/settings-manager.js";

const [projectRoot, agentDir] = process.argv.slice(1);
const resourceLoaderOptions = {
	noExtensions: true,
	noPromptTemplates: true,
	noThemes: true,
	noContextFiles: true,
};
const shippedServices = await createAgentSessionServices({
	cwd: projectRoot,
	agentDir,
	resourceLoaderOptions,
	telemetryDisabled: true,
});
const shippedSkills = shippedServices.resourceLoader.getSkills().skills;
const shippedNames = new Set(shippedSkills.map((skill) => skill.name));
if (shippedNames.has("prime-intellect")) {
	throw new Error("removed Prime Intellect skill remains in the resolved skill set");
}
if (shippedNames.has("websearch")) {
	throw new Error("remote-backed websearch loaded under shipped Synth settings");
}
for (const integration of ["linear", "notion"]) {
	if (shippedNames.has(integration)) {
		throw new Error(`unauthenticated ${integration} skill loaded in the runtime resource set`);
	}
	if (!existsSync(join(projectRoot, "packages/coding-agent/skills", integration))) {
		throw new Error(`${integration} implementation was removed instead of remaining auth-gated`);
	}
}
for (const removedPath of [
	join(projectRoot, "packages/coding-agent/skills/prime-intellect"),
	join(projectRoot, "packages/coding-agent/dist/skills/prime-intellect"),
]) {
	if (existsSync(removedPath)) {
		throw new Error(`removed Prime Intellect skill still exists at ${removedPath}`);
	}
}

const optInServices = await createAgentSessionServices({
	cwd: projectRoot,
	agentDir,
	resourceLoaderOptions,
	settingsManager: SettingsManager.inMemory({ bundledSkills: { websearch: true } }),
	telemetryDisabled: true,
});
const optInSkills = optInServices.resourceLoader.getSkills().skills;
const websearch = optInSkills.find((skill) => skill.name === "websearch");
if (websearch?.kind !== "python" || websearch.python.importName !== "websearch") {
	throw new Error("explicit websearch opt-in did not resolve the bundled Python skill");
}
if (optInSkills.some((skill) => skill.name === "prime-intellect")) {
	throw new Error("websearch opt-in restored the removed Prime Intellect skill");
}
if (optInSkills.some((skill) => skill.name === "linear" || skill.name === "notion")) {
	throw new Error("websearch opt-in bypassed MCP authentication gates");
}
' "${synth_root}" "${demo_agent_dir}"
)
printf 'synth-demo: Phase 1 A6 remote-backed and auth-gated skill defaults PASS\n'

phase2_telemetry="${demo_dir}/phase2-telemetry.jsonl"
printf 'synth-demo: pre-run process receipt phase2_driver=absent bridge_fixture=absent\n'
(
	cd "${synth_root}"
	SYNTH_PHASE2_TELEMETRY_OUT="${phase2_telemetry}" \
		"${tsx_path}" "${phase2_driver_path}"
)
(
	cd "${synth_root}"
	uv run --project . --no-sync synth telemetry validate "${phase2_telemetry}"
)
printf 'synth-demo: Phase 2 mirrored telemetry validation PASS\n'

phase3_telemetry="${demo_dir}/phase3-telemetry.jsonl"
(
	cd "${synth_root}"
	SYNTH_PHASE3_TELEMETRY_OUT="${phase3_telemetry}" \
		"${tsx_path}" "${phase3_driver_path}"
)
(
	cd "${synth_root}"
	uv run --project . --no-sync synth telemetry validate "${phase3_telemetry}"
)
printf 'synth-demo: Phase 3 BATS transcript and mirrored telemetry PASS\n'

(
	cd "${synth_root}"
	"${tsx_path}" "${phase4_driver_path}"
)
printf 'synth-demo: Phase 4 two-session ReasoningBank loop PASS\n'

memory_redaction_output="$(
	cd "${synth_root}"
	uv run --project . --no-sync python -m local_memory.demo
)"
printf '%s\n' "${memory_redaction_output}"
if ! grep -q 'redacted_log_lines=5000 terminal_exception=preserved' <<<"${memory_redaction_output}"; then
	printf 'synth-demo: Phase 4 observation redaction proof omitted terminal evidence\n' >&2
	exit 1
fi
printf 'synth-demo: Phase 4 5000-line observation redaction PASS\n'
if ! grep -q 'memory_reinforcement=rank_changed failed_confidence=0.333 succeeded_confidence=0.667' \
	<<<"${memory_redaction_output}"; then
	printf 'synth-demo: Phase 4 memory outcome reinforcement did not change tied ranking\n' >&2
	exit 1
fi
printf 'synth-demo: Phase 4 strategy-memory reinforcement PASS\n'

(
	cd "${synth_root}"
	"${tsx_path}" "${phase5_driver_path}"
)
printf 'synth-demo: Phase 5 strategic verifier and hybrid batch PASS\n'

optimizer_run_id="synth-demo-$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
optimizer_branch="synth/optimizer/${optimizer_run_id}"
optimizer_output="$(
	cd "${synth_root}"
	UV_OFFLINE=1 uv run --project . --no-sync synth optimize \
		--config "${optimizer_config_path}" \
		--run-id "${optimizer_run_id}" \
		--branch "${optimizer_branch}"
)"
printf '%s\n' "${optimizer_output}"
optimizer_report_path="$(awk -F= '$1 == "report" { print substr($0, index($0, "=") + 1) }' <<<"${optimizer_output}")"
optimizer_gated_commit="$(awk -F= '$1 == "gated_commit" { print $2 }' <<<"${optimizer_output}")"
if [[ -z "${optimizer_report_path}" || -z "${optimizer_gated_commit}" ]]; then
	printf 'synth-demo: optimizer output omitted its report or gated commit receipt\n' >&2
	exit 1
fi
optimizer_run_dir="${synth_root}/.synth/optimizer-runs/${optimizer_run_id}"
while IFS= read -r worktree_line; do
	[[ "${worktree_line}" == worktree\ * ]] || continue
	worktree_path="${worktree_line#worktree }"
	if [[ "${worktree_path}" == "${optimizer_run_dir}" || "${worktree_path}" == "${optimizer_run_dir}/"* ]]; then
		printf 'synth-demo: optimizer left a managed worktree registered: %s\n' "${worktree_path}" >&2
		exit 1
	fi
done < <(git -C "${synth_root}" worktree list --porcelain)
printf 'synth-demo: Phase 6 control-plane protection and worktree teardown PASS\n'
node --input-type=module --eval '
import { readFileSync } from "node:fs";

const [reportPath, runId, branch, baseRevision, gatedCommit] = process.argv.slice(1);
const report = JSON.parse(readFileSync(reportPath, "utf8"));
if (
	report.schema_version !== "synth-optimizer-report/v1" ||
	report.run_id !== runId ||
	report.optimizer_branch !== branch ||
	report.base_revision !== baseRevision ||
	report.gated_commit !== gatedCommit ||
	report.gated_commit === report.base_revision ||
	report.development_score !== 1 ||
	report.fix_rate !== 1 ||
	report.regression_rate !== 0 ||
	report.fixed !== report.baseline_failures ||
	report.fixed < 1 ||
	report.regressed !== 0
) {
	throw new Error(`optimizer report did not prove a strict, regression-free improvement: ${JSON.stringify(report)}`);
}
' "${optimizer_report_path}" "${optimizer_run_id}" "${optimizer_branch}" "${source_head}" "${optimizer_gated_commit}"
if [[ "$(git -C "${synth_root}" rev-parse "refs/heads/${optimizer_branch}")" != "${optimizer_gated_commit}" ]] || \
	[[ "$(git -C "${synth_root}" rev-parse "${optimizer_gated_commit}^")" != "${source_head}" ]]; then
	printf 'synth-demo: optimizer branch does not point at a direct gated child of the source revision\n' >&2
	exit 1
fi
if [[ "$(git -C "${synth_root}" diff --name-only "${source_head}" "${optimizer_gated_commit}")" != '.prime/agent/skills/synth-harness/SKILL.md' ]]; then
	printf 'synth-demo: optimizer gated commit escaped its declared skill surface\n' >&2
	exit 1
fi
if [[ "$(git -C "${synth_root}" branch --show-current)" != "${source_branch}" ]] || \
	[[ "$(git -C "${synth_root}" rev-parse HEAD)" != "${source_head}" ]] || \
	[[ -n "$(git -C "${synth_root}" status --porcelain=v1 --untracked-files=all)" ]]; then
	printf 'synth-demo: optimizer changed the source branch, HEAD, or worktree\n' >&2
	exit 1
fi
printf 'synth-demo: Phase 6 AutoSaddler gated commit PASS branch=%s fix_rate=1 regression_rate=0\n' \
	"${optimizer_branch}"

(
	cd "${synth_root}"
	TSX_TSCONFIG_PATH="${synth_root}/tsconfig.base.json" \
		"${tsx_path}" "${phase7_driver_path}"
)
printf 'synth-demo: Phase 7 dynamic skill compiler and problem framing PASS\n'

(
	cd "${synth_root}"
	PI_OFFLINE=1 UV_OFFLINE=1 \
		uv run --project . --no-sync pytest -q "${phase8_failure_tests_path}"
)
printf 'synth-demo: Phase 8 four bounded failure injections PASS\n'

(
	cd "${synth_root}"
	PI_OFFLINE=1 UV_OFFLINE=1 "${tsx_path}" "${phase8_compiler_crash_driver_path}"
)
printf 'synth-demo: Phase 8 detached compiler crash receipt PASS\n'

(
	cd "${synth_root}"
	"${tsx_path}" "${config_doc_generator_path}" --check
)
printf 'synth-demo: Phase 8 generated configuration reference PASS\n'

bridge_output="$({
	printf '%s\n' 'not-json'
	printf '%s\n' '{"id":"demo-ping","command":"ping","params":{"protocolVersion":1}}'
	printf '%s\n' '{"id":"demo-memory","command":"memory.query","params":{"query":"toy","taskDescription":"coding","k":3,"minScore":0.35,"statuses":["success","fail"],"itemTypes":["strategy"]}}'
} | (
	cd "${synth_root}"
	uv run --project . --no-sync synth-bridge
))"
printf '%s\n' "${bridge_output}"
node --input-type=module --eval '
const responses = process.argv[1].split("\n").filter(Boolean).map((line) => JSON.parse(line));
if (
	responses.length !== 3 ||
	responses[0]?.id !== null ||
	responses[0]?.ok !== false ||
	responses[0]?.error?.code !== "invalid_json" ||
	responses[1]?.id !== "demo-ping" ||
	responses[1]?.ok !== true ||
	responses[1]?.result?.protocolVersion !== 1 ||
	responses[1]?.result?.memoryAvailable !== false ||
	responses[2]?.id !== "demo-memory" ||
	responses[2]?.ok !== false ||
	responses[2]?.error?.code !== "feature_disabled"
) {
	throw new Error("Synth bridge framing or Phase 4 capability boundary changed");
}
' "${bridge_output}"
printf 'synth-demo: Phase 2 strict JSON-lines framing PASS\n'

embed_output="$(
	cd "${synth_root}"
	uv run --project . --no-sync synth embed --kind query 'Phase 2 local embedding proof'
)"
node --input-type=module --eval '
const result = JSON.parse(process.argv[1]);
if (
	result.model !== "BAAI/bge-small-en-v1.5" ||
	result.dimension !== 384 ||
	!Array.isArray(result.embeddings) ||
	result.embeddings.length !== 1 ||
	result.embeddings[0].length !== 384 ||
	!result.embeddings[0].every(Number.isFinite)
) {
	throw new Error("prepared local embedder returned an invalid vector");
}
' "${embed_output}"
printf 'synth-demo: Phase 2 prepared local embedder PASS\n'

(
	cd "${synth_root}"
	"${wiring_script}"
)
printf 'synth-demo: FULL LOOP PASS memory=distilled+retrieved budgets=visible+enforced verifier=pivot optimizer=gated skill=compiled+promoted+invoked\n'
printf 'synth-demo: Phase 2 two-head wiring PASS\n'
