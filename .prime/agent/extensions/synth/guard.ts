import { lstatSync, readdirSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import { isToolCallEventType, type ToolCallEvent } from "@earendil-works/pi-coding-agent";

const ALLOWED_DATA_ROOTS = [".synth", ".prime/agent/skills", ".prime/agent/harness"];
const OS_PROTECTED_CONTROL_ROOTS = ["packages", "prime-agent-runtime", ".prime/agent/extensions"];
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
	/(?:open\s*\([^)]*,\s*["'][wax+]|\.write_text\s*\(|\.write_bytes\s*\(|\.chmod\s*\(|\.unlink\s*\(|\.rename\s*\(|\.replace\s*\(|shutil\.(?:copy|move|rmtree)|os\.(?:chmod|remove|rename|replace|unlink))/;

type ManagedWorkspaceKind = "batch" | "optimizer";

function managedWorkspaceKind(environment: NodeJS.ProcessEnv): ManagedWorkspaceKind | undefined {
	const value = environment.SYNTH_MANAGED_WORKSPACE;
	return value === "batch" || value === "optimizer" ? value : undefined;
}

function errorCode(error: unknown): string | undefined {
	if (typeof error !== "object" || error === null || !("code" in error)) return undefined;
	return typeof error.code === "string" ? error.code : undefined;
}

function displayPath(cwd: string, path: string): string {
	return relative(cwd, path).replaceAll("\\", "/") || ".";
}

function unsafeControlPath(cwd: string, root: string): string | undefined {
	let component = cwd;
	for (const part of root.split("/")) {
		component = join(component, part);
		try {
			if (lstatSync(component).isSymbolicLink()) {
				return `${displayPath(cwd, component)} (symlink)`;
			}
		} catch (error) {
			if (errorCode(error) === "ENOENT") return undefined;
			return `${displayPath(cwd, component)} (scan failed)`;
		}
	}

	const pending = [resolve(cwd, root)];
	while (pending.length > 0) {
		const path = pending.pop();
		if (!path) break;
		try {
			const status = lstatSync(path);
			if (status.isSymbolicLink()) return `${displayPath(cwd, path)} (symlink)`;
			if ((status.mode & 0o222) !== 0) return displayPath(cwd, path);
			if (status.isDirectory()) {
				for (const name of readdirSync(path)) pending.push(join(path, name));
			} else if (!status.isFile()) {
				return `${displayPath(cwd, path)} (unsupported file type)`;
			}
		} catch {
			return `${displayPath(cwd, path)} (scan failed)`;
		}
	}
	return undefined;
}

export function managedControlPlaneWarning(
	cwd: string,
	environment: NodeJS.ProcessEnv = process.env,
): string | undefined {
	const kind = managedWorkspaceKind(environment);
	if (!kind) return undefined;
	const unsafe = OS_PROTECTED_CONTROL_ROOTS.flatMap((root) => {
		const path = unsafeControlPath(cwd, root);
		return path ? [path] : [];
	});
	if (unsafe.length === 0) return undefined;
	return `managed ${kind} workspace has writable or unsafe control-plane entries: ${unsafe.join(
		", ",
	)}; chmod a-w protection is incomplete`;
}

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

function containsRelativePath(text: string, path: string): boolean {
	const beforeBoundary = " \t\r\n\"'`([{=,:;";
	const afterBoundary = `${beforeBoundary})]}`;
	for (const candidate of [path, `./${path}`]) {
		let offset = 0;
		while (offset < text.length) {
			const index = text.indexOf(candidate, offset);
			if (index < 0) break;
			const before = index === 0 ? undefined : text[index - 1];
			const afterIndex = index + candidate.length;
			const after = afterIndex === text.length ? undefined : text[afterIndex];
			if (
				(before === undefined || beforeBoundary.includes(before)) &&
				(after === undefined || after === "/" || afterBoundary.includes(after))
			) {
				return true;
			}
			offset = index + candidate.length;
		}
	}
	return false;
}

function mentionsProtectedPath(text: string, cwd: string): boolean {
	const normalized = text.replaceAll("\\", "/");
	for (const root of PROTECTED_CONTROL_ROOTS) {
		if (containsRelativePath(normalized, root)) {
			return true;
		}
		const absolute = resolve(cwd, root).replaceAll("\\", "/");
		if (normalized.includes(absolute)) {
			return true;
		}
	}
	for (const file of PROTECTED_CONTROL_FILES) {
		if (
			containsRelativePath(normalized, file) ||
			normalized.includes(resolve(cwd, file).replaceAll("\\", "/"))
		) {
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
