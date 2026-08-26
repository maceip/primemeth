import { resolve } from "node:path";
import { getAgentDir } from "../packages/coding-agent/src/config.js";
import { ensureKernelPython } from "../packages/coding-agent/src/core/kernel/bootstrap.js";
import { DefaultResourceLoader } from "../packages/coding-agent/src/core/resource-loader.js";
import { getPythonSkillRuntimeInfo } from "../packages/coding-agent/src/core/skills.js";

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

try {
	const cwd = resolve(process.argv[2] ?? process.cwd());
	const loader = new DefaultResourceLoader({
		agentDir: getAgentDir(),
		cwd,
		noContextFiles: true,
		noExtensions: true,
		noPromptTemplates: true,
		noThemes: true,
	});
	await loader.reload();
	const skillResult = loader.getSkills();
	const errors = skillResult.diagnostics.filter((diagnostic) => diagnostic.type === "error");
	if (errors.length > 0) {
		throw new Error(errors.map((diagnostic) => `${diagnostic.path}: ${diagnostic.message}`).join("\n"));
	}
	const pythonSkills = getPythonSkillRuntimeInfo(skillResult.skills);
	const python = await ensureKernelPython({
		onProgress: (message) => console.error(message),
		pythonSkills,
	});
	console.log(`kernel python: ${python}`);
	console.log(`kernel python skills: ${pythonSkills.length}`);
} catch (error) {
	console.error(`bootstrap-synth-kernel: ${errorMessage(error)}`);
	process.exitCode = 1;
}
