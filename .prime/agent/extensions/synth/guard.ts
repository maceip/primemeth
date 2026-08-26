import { isAbsolute, relative, resolve } from "node:path";
import { isToolCallEventType, type ToolCallEvent } from "@earendil-works/pi-coding-agent";

const ALLOWED_DATA_ROOTS = [".synth", ".prime/agent/skills", ".prime/agent/harness"];
const PROTECTED_CONTROL_ROOTS = [
	".prime/agent/extensions",
	"packages",
	"prime-agent-runtime",
	"local_memory",
	"middleware",
	"optimizer",
	"synth",
	"scripts",
];
const PROTECTED_CONTROL_FILES = new Set([
	"AGENTS.md",
	"ARCHITECTURE_NOTES.md",
	"DECISIONS.md",
	"INTEGRATION_MAP.md",
	"PORTING.md",
	"pyproject.toml",
	"synth.json",
	"uv.lock",
	".prime/agent/models.json",
	".prime/agent/settings.json",
]);
const MUTATING_SHELL =
	/(?:^|[;&|]\s*)(?:apply_patch|chmod|chown|cp|git\s+(?:apply|checkout|clean|reset)|install|mkdir|mv|perl\s+-pi|rm|sed\s+-i|tee|touch)(?:\s|$)|(?:^|\s)(?:>|>>|1>|2>)/;
const CONTROL_PLANE_SHELL =
	/(?:^|[;&|]\s*)(?:apply_patch|git\s+(?:add|am|apply|checkout|cherry-pick|clean|commit|merge|mv|rebase|reset|restore|rm|stash|switch|tag|worktree)|npm\s+(?:install|uninstall|update)|pnpm\s+(?:add|install|remove|update)|uv\s+(?:add|lock|remove|sync)|yarn\s+(?:add|install|remove|upgrade))(?:\s|$)/;
const MUTATING_PYTHON =
	/(?:open\s*\([^)]*,\s*["'][wax+]|\.write_text\s*\(|\.write_bytes\s*\(|\.unlink\s*\(|\.rename\s*\(|\.replace\s*\(|shutil\.(?:copy|move|rmtree)|os\.(?:remove|rename|replace|unlink))/;

function isWithin(parent: string, candidate: string): boolean {
	const pathFromParent = relative(parent, candidate);
	return pathFromParent === "" || (!pathFromParent.startsWith("..") && !isAbsolute(pathFromParent));
}

function protectedPath(path: string, cwd: string): boolean {
	const absolute = resolve(cwd, path);
	for (const allowed of ALLOWED_DATA_ROOTS) {
		if (isWithin(resolve(cwd, allowed), absolute)) {
			return false;
		}
	}
	for (const root of PROTECTED_CONTROL_ROOTS) {
		if (isWithin(resolve(cwd, root), absolute)) {
			return true;
		}
	}
	for (const file of PROTECTED_CONTROL_FILES) {
		if (absolute === resolve(cwd, file)) {
			return true;
		}
	}
	return false;
}

function mentionsProtectedPath(text: string, cwd: string): boolean {
	const normalized = text.replaceAll("\\", "/");
	for (const root of PROTECTED_CONTROL_ROOTS) {
		if (normalized.includes(root)) {
			return true;
		}
		const absolute = resolve(cwd, root).replaceAll("\\", "/");
		if (normalized.includes(absolute)) {
			return true;
		}
	}
	for (const file of PROTECTED_CONTROL_FILES) {
		if (normalized.includes(file) || normalized.includes(resolve(cwd, file).replaceAll("\\", "/"))) {
			return true;
		}
	}
	return false;
}

export function controlPlaneBlockReason(event: ToolCallEvent, cwd: string): string | undefined {
	if (isToolCallEventType("edit", event) && protectedPath(event.input.path, cwd)) {
		return `Synth runtime guard: control-plane edits are offline-only (${event.input.path}).`;
	}
	if (isToolCallEventType("bash", event) && CONTROL_PLANE_SHELL.test(event.input.command)) {
		return "Synth runtime guard: repository and dependency mutations are offline-only.";
	}
	if (
		isToolCallEventType("bash", event) &&
		mentionsProtectedPath(event.input.command, cwd) &&
		MUTATING_SHELL.test(event.input.command)
	) {
		return "Synth runtime guard: the shell command attempts to mutate the control plane.";
	}
	if (
		isToolCallEventType("ipython", event) &&
		mentionsProtectedPath(event.input.code, cwd) &&
		MUTATING_PYTHON.test(event.input.code)
	) {
		return "Synth runtime guard: the Python cell attempts to mutate the control plane.";
	}
	return undefined;
}
