import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { getAgentDir } from "../packages/coding-agent/src/config.js";
import { createAgentSessionServices } from "../packages/coding-agent/src/core/agent-session-services.js";
import { ensureKernelPython } from "../packages/coding-agent/src/core/kernel/bootstrap.js";
import {
	getPythonSkillRuntimeInfo,
	type PythonSkillRuntimeInfo,
} from "../packages/coding-agent/src/core/skills.js";

interface BootstrapReceiptSkill {
	importName: string;
	packagePath: string;
	pyprojectPath: string;
	pyprojectHash: string;
}

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBootstrapReceiptSkill(value: unknown): value is BootstrapReceiptSkill {
	return (
		isRecord(value) &&
		typeof value.importName === "string" &&
		typeof value.packagePath === "string" &&
		typeof value.pyprojectPath === "string" &&
		typeof value.pyprojectHash === "string"
	);
}

function pyprojectHash(pyprojectPath: string): string {
	return `sha256:${createHash("sha256").update(readFileSync(pyprojectPath)).digest("hex")}`;
}

function verifyBootstrapReceipt(python: string, pythonSkills: readonly PythonSkillRuntimeInfo[]): void {
	const receiptPath = join(dirname(dirname(python)), ".bootstrap-version");
	const receipt: unknown = JSON.parse(readFileSync(receiptPath, "utf8"));
	if (!isRecord(receipt) || !Array.isArray(receipt.pythonSkills)) {
		throw new Error(`kernel bootstrap receipt has no Python skill set: ${receiptPath}`);
	}
	const installedSkills = receipt.pythonSkills.filter(isBootstrapReceiptSkill);
	if (installedSkills.length !== receipt.pythonSkills.length) {
		throw new Error(`kernel bootstrap receipt has invalid Python skill entries: ${receiptPath}`);
	}

	for (const skill of pythonSkills) {
		const packagePath = resolve(skill.packagePath);
		const pyprojectPath = resolve(skill.pyprojectPath);
		const installed = installedSkills.find(
			(candidate) => candidate.importName === skill.importName && candidate.packagePath === packagePath,
		);
		if (
			!installed ||
			installed.pyprojectPath !== pyprojectPath ||
			installed.pyprojectHash !== pyprojectHash(pyprojectPath)
		) {
			throw new Error(
				`enabled Python skill is absent or stale in the bootstrap receipt: ${skill.name} (${skill.importName})`,
			);
		}
	}
}

function verifyPythonSkillImports(python: string, importNames: readonly string[]): void {
	const script = [
		"import importlib",
		"import sys",
		`names = ${JSON.stringify(importNames)}`,
		"failures = []",
		"for name in names:",
		"    try:",
		"        importlib.import_module(name)",
		"    except BaseException as error:",
		'        failures.append(f"{name}: {type(error).__name__}: {error}")',
		"if failures:",
		'    print("\\n".join(failures), file=sys.stderr)',
		"    raise SystemExit(1)",
	].join("\n");
	const result = spawnSync(python, ["-c", script], {
		encoding: "utf8",
		stdio: ["ignore", "pipe", "pipe"],
	});
	if (result.status === 0) {
		return;
	}
	const detail = result.error?.message || result.stderr.trim() || `exit code ${result.status ?? "unknown"}`;
	throw new Error(`enabled Python skill import verification failed: ${detail}`);
}

try {
	const cwd = resolve(process.argv[2] ?? process.cwd());
	const services = await createAgentSessionServices({
		agentDir: getAgentDir(),
		cwd,
		resourceLoaderOptions: {
			noContextFiles: true,
			noExtensions: true,
			noPromptTemplates: true,
			noThemes: true,
		},
		telemetryDisabled: true,
	});
	const skillResult = services.resourceLoader.getSkills();
	const errors = skillResult.diagnostics.filter((diagnostic) => diagnostic.type === "error");
	if (errors.length > 0) {
		throw new Error(errors.map((diagnostic) => `${diagnostic.path}: ${diagnostic.message}`).join("\n"));
	}
	const pythonSkills = getPythonSkillRuntimeInfo(skillResult.skills);
	const skillInstallWarnings: string[] = [];
	const python = await ensureKernelPython({
		onProgress: (message) => {
			console.error(message);
			if (message.startsWith("Warning: Python skill ")) {
				skillInstallWarnings.push(message);
			}
		},
		pythonSkills,
	});
	verifyBootstrapReceipt(python, pythonSkills);
	if (skillInstallWarnings.length > 0) {
		throw new Error(`enabled Python skill installation failed:\n${skillInstallWarnings.join("\n")}`);
	}
	verifyPythonSkillImports(
		python,
		pythonSkills.map((skill) => skill.importName),
	);
	console.log(`kernel python: ${python}`);
	console.log(`kernel python skills: ${pythonSkills.length}`);
	console.log("kernel python skill imports: verified");
} catch (error) {
	console.error(`bootstrap-synth-kernel: ${errorMessage(error)}`);
	process.exitCode = 1;
}
