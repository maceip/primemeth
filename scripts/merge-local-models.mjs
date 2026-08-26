#!/usr/bin/env node

import { randomUUID } from "node:crypto";
import {
	closeSync,
	constants,
	fchmodSync,
	fstatSync,
	fsyncSync,
	lstatSync,
	mkdirSync,
	openSync,
	readFileSync,
	renameSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";

const scriptDir = dirname(fileURLToPath(import.meta.url));
const defaultTemplatePath = join(scriptDir, "..", ".prime", "agent", "models.json");

function isNodeError(error, code) {
	return error instanceof Error && "code" in error && error.code === code;
}

function isRecord(value) {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseJsonc(raw, label) {
	const stripped = raw
		.replace(/"(?:\\.|[^"\\])*"|\/\/[^\n]*/g, (match) => (match[0] === '"' ? match : ""))
		.replace(/"(?:\\.|[^"\\])*"|,(\s*[}\]])/g, (match, tail) => tail ?? (match[0] === '"' ? match : ""));
	try {
		return JSON.parse(stripped);
	} catch (error) {
		const detail = error instanceof Error ? error.message : String(error);
		throw new Error(`${label} is not valid runtime JSONC: ${detail}`);
	}
}

function validateModel(model, pathLabel) {
	if (!isRecord(model)) {
		throw new Error(`${pathLabel} must be an object`);
	}
	if (typeof model.id !== "string" || model.id.length === 0) {
		throw new Error(`${pathLabel}.id must be a nonempty string`);
	}
}

function validateCatalog(value, label, requireCompleteProviders) {
	if (!isRecord(value)) {
		throw new Error(`${label} root must be an object`);
	}
	if (!isRecord(value.providers)) {
		throw new Error(`${label}.providers must be an object`);
	}

	for (const [providerName, provider] of Object.entries(value.providers)) {
		const providerLabel = `${label}.providers.${providerName}`;
		if (!isRecord(provider)) {
			throw new Error(`${providerLabel} must be an object`);
		}
		if (provider.models !== undefined && !Array.isArray(provider.models)) {
			throw new Error(`${providerLabel}.models must be an array`);
		}
		const modelIds = new Set();
		for (const [index, model] of (provider.models ?? []).entries()) {
			validateModel(model, `${providerLabel}.models[${index}]`);
			if (requireCompleteProviders && modelIds.has(model.id)) {
				throw new Error(`${providerLabel}.models contains duplicate id ${model.id}`);
			}
			modelIds.add(model.id);
		}
		if (requireCompleteProviders) {
			for (const key of ["baseUrl", "api", "apiKey"]) {
				if (typeof provider[key] !== "string" || provider[key].length === 0) {
					throw new Error(`${providerLabel}.${key} must be a nonempty string`);
				}
			}
			if (!Array.isArray(provider.models) || provider.models.length === 0) {
				throw new Error(`${providerLabel}.models must contain at least one model`);
			}
		}
	}

	return value;
}

function defineOwn(target, key, value) {
	Object.defineProperty(target, key, {
		configurable: true,
		enumerable: true,
		value,
		writable: true,
	});
}

function mergeCatalog(target, template) {
	const receipts = [];
	let changed = false;

	for (const [providerName, templateProvider] of Object.entries(template.providers)) {
		if (!Object.hasOwn(target.providers, providerName)) {
			defineOwn(target.providers, providerName, structuredClone(templateProvider));
			receipts.push(`models.json: added provider ${providerName}`);
			changed = true;
			continue;
		}

		const targetProvider = target.providers[providerName];
		for (const [key, templateValue] of Object.entries(templateProvider)) {
			if (key === "models") {
				continue;
			}
			if (!Object.hasOwn(targetProvider, key)) {
				defineOwn(targetProvider, key, structuredClone(templateValue));
				receipts.push(`models.json: added ${providerName}.${key}`);
				changed = true;
			} else if (!isDeepStrictEqual(targetProvider[key], templateValue)) {
				receipts.push(`models.json: preserved conflict ${providerName}.${key}`);
			}
		}

		const templateModels = templateProvider.models ?? [];
		if (!Object.hasOwn(targetProvider, "models")) {
			defineOwn(targetProvider, "models", []);
		}
		const targetModels = targetProvider.models;
		const targetById = new Map();
		for (const model of targetModels) {
			if (!targetById.has(model.id)) {
				targetById.set(model.id, model);
			}
		}
		for (const templateModel of templateModels) {
			const existingModel = targetById.get(templateModel.id);
			if (existingModel === undefined) {
				targetModels.push(structuredClone(templateModel));
				targetById.set(templateModel.id, templateModel);
				receipts.push(`models.json: added ${providerName} model ${templateModel.id}`);
				changed = true;
			} else if (!isDeepStrictEqual(existingModel, templateModel)) {
				receipts.push(`models.json: preserved conflict ${providerName} model ${templateModel.id}`);
			}
		}
	}

	return { changed, receipts };
}

function resolveAgentDir() {
	const configured = process.env.PRIME_AGENT_CODING_AGENT_DIR;
	if (configured) {
		if (configured === "~") {
			return homedir();
		}
		if (configured.startsWith("~/")) {
			return `${homedir()}${configured.slice(1)}`;
		}
		return configured;
	}
	return join(homedir(), ".prime", "agent");
}

function parseArgs(argv) {
	let templatePath = defaultTemplatePath;
	for (let index = 0; index < argv.length; index++) {
		if (argv[index] === "--template" && index + 1 < argv.length) {
			templatePath = argv[index + 1];
			index++;
			continue;
		}
		throw new Error(`unknown or incomplete argument: ${argv[index]}`);
	}
	return { templatePath };
}

function readTemplate(templatePath) {
	let templateStat;
	try {
		templateStat = lstatSync(templatePath);
	} catch (error) {
		if (isNodeError(error, "ENOENT")) {
			throw new Error(`template does not exist: ${templatePath}`);
		}
		throw error;
	}
	if (!templateStat.isFile() || templateStat.isSymbolicLink()) {
		throw new Error(`template must be a regular file: ${templatePath}`);
	}
	return validateCatalog(parseJsonc(readFileSync(templatePath, "utf8"), "template"), "template", true);
}

function identity(stat) {
	return [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs].map(String).join(":");
}

function readDestination(destinationPath) {
	let pathStat;
	try {
		pathStat = lstatSync(destinationPath, { bigint: true });
	} catch (error) {
		if (isNodeError(error, "ENOENT")) {
			return { exists: false, identity: undefined, mode: 0o600, raw: undefined };
		}
		throw error;
	}
	if (!pathStat.isFile() || pathStat.isSymbolicLink()) {
		throw new Error(`destination must be a regular file, not a symlink or other file type: ${destinationPath}`);
	}

	const noFollow = typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0;
	const fd = openSync(destinationPath, constants.O_RDONLY | noFollow);
	try {
		const fdStat = fstatSync(fd, { bigint: true });
		if (!fdStat.isFile() || identity(fdStat) !== identity(pathStat)) {
			throw new Error(`destination changed while it was being opened: ${destinationPath}`);
		}
		return {
			exists: true,
			identity: identity(fdStat),
			mode: Number(fdStat.mode & 0o777n),
			raw: readFileSync(fd, "utf8"),
		};
	} finally {
		closeSync(fd);
	}
}

function assertDestinationUnchanged(destinationPath, initial) {
	let current;
	try {
		current = lstatSync(destinationPath, { bigint: true });
	} catch (error) {
		if (isNodeError(error, "ENOENT")) {
			if (!initial.exists) {
				return;
			}
			throw new Error(`destination disappeared before replacement: ${destinationPath}`);
		}
		throw error;
	}
	if (!initial.exists) {
		throw new Error(`destination appeared before creation: ${destinationPath}`);
	}
	if (!current.isFile() || current.isSymbolicLink() || identity(current) !== initial.identity) {
		throw new Error(`destination changed before replacement: ${destinationPath}`);
	}
}

function flushDirectory(directoryPath) {
	const fd = openSync(directoryPath, constants.O_RDONLY);
	try {
		fsyncSync(fd);
	} finally {
		closeSync(fd);
	}
}

function writeAtomic(destinationPath, initial, content) {
	const directoryPath = dirname(destinationPath);
	mkdirSync(directoryPath, { mode: 0o700, recursive: true });
	const temporaryPath = join(directoryPath, `.${basename(destinationPath)}.synth-${process.pid}-${randomUUID()}`);
	const noFollow = typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0;
	let fd;
	try {
		fd = openSync(
			temporaryPath,
			constants.O_CREAT | constants.O_EXCL | constants.O_WRONLY | noFollow,
			initial.mode,
		);
		fchmodSync(fd, initial.mode);
		writeFileSync(fd, content, "utf8");
		fsyncSync(fd);
		closeSync(fd);
		fd = undefined;

		assertDestinationUnchanged(destinationPath, initial);
		renameSync(temporaryPath, destinationPath);
		flushDirectory(directoryPath);
	} finally {
		if (fd !== undefined) {
			closeSync(fd);
		}
		rmSync(temporaryPath, { force: true });
	}
}

function main() {
	const { templatePath } = parseArgs(process.argv.slice(2));
	const template = readTemplate(templatePath);
	const destinationPath = join(resolveAgentDir(), "models.json");
	const initial = readDestination(destinationPath);
	const target = initial.exists
		? validateCatalog(parseJsonc(initial.raw, "models.json"), "models.json", false)
		: { providers: {} };
	const result = mergeCatalog(target, template);

	for (const receipt of result.receipts) {
		console.log(receipt);
	}
	if (!result.changed) {
		console.log("models.json: no changes");
		return;
	}

	writeAtomic(destinationPath, initial, `${JSON.stringify(target, null, 2)}\n`);
	console.log(`models.json: updated ${destinationPath}`);
}

try {
	main();
} catch (error) {
	const detail = error instanceof Error ? error.message : String(error);
	console.error(`merge-local-models: ${detail}`);
	process.exitCode = 1;
}
