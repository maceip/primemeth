import assert from "node:assert/strict";
import {
	copyFileSync,
	existsSync,
	mkdtempSync,
	mkdirSync,
	readFileSync,
	rmSync,
	statSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { fauxAssistantMessage, type FauxResponseFactory } from "@earendil-works/pi-ai";
import synthExtension from "../.prime/agent/extensions/synth/index.js";
import { LESSONS_HEADING } from "../.prime/agent/extensions/synth/memory.js";
import { emitSessionShutdownEvent } from "../packages/coding-agent/src/core/extensions/runner.js";
import { createHarness, type Harness } from "../packages/coding-agent/test/suite/harness.js";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const preparedEmbedCache = join(repositoryRoot, ".synth", "models", "fastembed");
const demoDir = mkdtempSync(join(tmpdir(), "synth-phase4-demo-"));
const fixturePath = join(demoDir, "memory-fixture.json");
const databaseSnapshot = join(demoDir, "synth.db");
const lessonTitle = "Load SQLite extensions before creating virtual tables";
const lessonDescription = "Initialize sqlite-vec before applying any vec0 schema.";
const lessonContent =
	"Load the vector extension on each connection before creating or querying virtual tables, then verify the schema through that initialized connection.";
const originalFixture = process.env.SYNTH_MEMORY_FIXTURE;
const originalEmbedCache = process.env.SYNTH_FASTEMBED_CACHE_DIR;

interface StoredMemoryRow {
	title: string;
	description: string;
	content: string;
	status: string;
	item_type: string;
}

function configText(): string {
	return `${JSON.stringify(
		{
			endpoint: {
				baseUrl: "http://localhost:8000/v1",
				model: "local-main",
				smallModel: "local-small",
				apiKeyEnv: "SYNTH_LOCAL_API_KEY",
			},
			telemetry: { dir: ".synth/telemetry" },
			memory: {
				enabled: true,
				k: 3,
				minScore: 0,
				db: ".synth/synth.db",
				embedModel: "BAAI/bge-small-en-v1.5",
				judgeModel: "small",
			},
		},
		null,
		2,
	)}\n`;
}

function prepareHarness(harness: Harness): void {
	writeFileSync(join(harness.tempDir, "synth.json"), configText());
	copyFileSync(join(repositoryRoot, "pyproject.toml"), join(harness.tempDir, "pyproject.toml"));
	symlinkSync(join(repositoryRoot, ".venv"), join(harness.tempDir, ".venv"), "dir");
}

function commandMessages(harness: Harness): string[] {
	return harness.session.messages.flatMap((message) => {
		if (
			message.role !== "custom" ||
			message.customType !== "synth.command" ||
			typeof message.content !== "string"
		) {
			return [];
		}
		return [message.content];
	});
}

function memoryRow(path: string): StoredMemoryRow | undefined {
	if (!existsSync(path)) {
		return undefined;
	}
	try {
		const database = new DatabaseSync(path, { readOnly: true });
		try {
			const row = database
				.prepare(
					"SELECT title, description, content, status, item_type FROM memory_items ORDER BY id LIMIT 1",
				)
				.get() as StoredMemoryRow | undefined;
			return row ? { ...row } : undefined;
		} finally {
			database.close();
		}
	} catch {
		return undefined;
	}
}

async function waitForMemory(path: string): Promise<StoredMemoryRow> {
	for (let attempt = 0; attempt < 300; attempt++) {
		const row = memoryRow(path);
		const walPath = `${path}-wal`;
		if (row && (!existsSync(walPath) || statSync(walPath).size === 0)) {
			return row;
		}
		await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 100));
	}
	throw new Error("timed out waiting for detached ReasoningBank distillation");
}

function persistedRetrieval(sessionFile: string): { taskQuery: string; section: string; itemIds: number[] } | undefined {
	for (const line of readFileSync(sessionFile, "utf8").split("\n")) {
		if (!line.trim()) continue;
		const value: unknown = JSON.parse(line);
		if (
			typeof value === "object" &&
			value !== null &&
			"type" in value &&
			value.type === "custom" &&
			"customType" in value &&
			value.customType === "synth.memory.retrieval" &&
			"data" in value &&
			typeof value.data === "object" &&
			value.data !== null &&
			"taskQuery" in value.data &&
			typeof value.data.taskQuery === "string" &&
			"section" in value.data &&
			typeof value.data.section === "string" &&
			"itemIds" in value.data &&
			Array.isArray(value.data.itemIds) &&
			value.data.itemIds.every((id) => Number.isSafeInteger(id))
		) {
			return {
				taskQuery: value.data.taskQuery,
				section: value.data.section,
				itemIds: value.data.itemIds.map(Number),
			};
		}
	}
	return undefined;
}

writeFileSync(
	fixturePath,
	`${JSON.stringify(
		{
			judge:
				'Thoughts: The attempt stopped after the extension-loading error and did not complete the requested repair.\nStatus: "failure"',
			distill: `# Memory Item 1\n## Title ${lessonTitle}\n## Description ${lessonDescription}\n## Content ${lessonContent}\n`,
		},
		null,
		2,
	)}\n`,
);
process.env.SYNTH_MEMORY_FIXTURE = fixturePath;
process.env.SYNTH_FASTEMBED_CACHE_DIR = preparedEmbedCache;

let firstHarness: Harness | undefined;
let secondHarness: Harness | undefined;
let firstShutdown = false;
let secondShutdown = false;
let completed = false;

try {
	firstHarness = await createHarness({ extensionFactories: [synthExtension], persistSession: true });
	prepareHarness(firstHarness);
	await firstHarness.session.bindExtensions({});
	firstHarness.setResponses([
		fauxAssistantMessage("The repair failed because sqlite-vec was not loaded before vec0 schema creation."),
	]);
	await firstHarness.session.prompt("Repair SQLite vec0 initialization after a no such module failure");
	const firstSessionFile = firstHarness.sessionManager.getSessionFile();
	assert.ok(firstSessionFile, "failing session did not persist a native transcript");

	await emitSessionShutdownEvent(firstHarness.session.extensionRunner, {
		type: "session_shutdown",
		reason: "quit",
	});
	firstShutdown = true;
	const firstSessionId = firstHarness.sessionManager.getSessionId();
	const telemetryPath = join(firstHarness.tempDir, ".synth", "telemetry", `${firstSessionId}.jsonl`);
	const telemetry = readFileSync(telemetryPath, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as { kind?: string; data?: Record<string, unknown> });
	const enqueue = telemetry.find((event) => event.data?.action === "memory.distill_enqueued");
	assert.ok(
		enqueue?.kind === "bridge" && typeof enqueue.data?.pid === "number" && enqueue.data.pid > 1,
		"shutdown did not record a detached distillation receipt",
	);

	const firstDatabase = join(firstHarness.tempDir, ".synth", "synth.db");
	const row = await waitForMemory(firstDatabase);
	assert.deepEqual(row, {
		title: lessonTitle,
		description: lessonDescription,
		content: lessonContent,
		status: "fail",
		item_type: "strategy",
	});
	copyFileSync(firstDatabase, databaseSnapshot);
	firstHarness.cleanup();
	firstHarness = undefined;

	secondHarness = await createHarness({ extensionFactories: [synthExtension], persistSession: true });
	prepareHarness(secondHarness);
	mkdirSync(join(secondHarness.tempDir, ".synth"), { recursive: true });
	copyFileSync(databaseSnapshot, join(secondHarness.tempDir, ".synth", "synth.db"));
	await secondHarness.session.bindExtensions({});

	await secondHarness.session.prompt("/synth memory list");
	const listOutput = commandMessages(secondHarness).at(-1) ?? "";
	assert.ok(listOutput.includes(lessonTitle) && listOutput.includes("[fail/strategy]"));
	await secondHarness.session.prompt("/synth memory search repair SQLite vec0 initialization");
	const searchOutput = commandMessages(secondHarness).at(-1) ?? "";
	assert.ok(searchOutput.includes(lessonTitle) && searchOutput.includes("score="));

	let capturedSystemPrompt = "";
	const capture: FauxResponseFactory = (context) => {
		capturedSystemPrompt = context.systemPrompt ?? "";
		return fauxAssistantMessage("The prior lesson was available before this attempt began.");
	};
	secondHarness.setResponses([capture]);
	const secondTask = "Repair another SQLite vec0 initialization failure by loading the extension first";
	await secondHarness.session.prompt(secondTask);
	const systemPrompt = capturedSystemPrompt;
	assert.ok(
		systemPrompt.includes(LESSONS_HEADING) &&
			systemPrompt.includes("Status: fail") &&
			systemPrompt.includes(lessonTitle) &&
			systemPrompt.includes(lessonContent),
		"session B provider request omitted the retrieved failed-session lesson",
	);

	const secondSessionFile = secondHarness.sessionManager.getSessionFile();
	assert.ok(secondSessionFile);
	const persisted = persistedRetrieval(secondSessionFile);
	assert.deepEqual(
		{
			taskQuery: persisted?.taskQuery,
			section: persisted?.section,
			itemCount: persisted?.itemIds.length,
		},
		{
			taskQuery: secondTask,
			section: systemPrompt.slice(systemPrompt.indexOf(LESSONS_HEADING)),
			itemCount: 1,
		},
	);

	await emitSessionShutdownEvent(secondHarness.session.extensionRunner, {
		type: "session_shutdown",
		reason: "reload",
	});
	secondShutdown = true;
	console.log(
		`synth-demo: Phase 4 ReasoningBank PASS failed_session=${firstSessionId} memories=1 retrieved=1`,
	);
	completed = true;
} finally {
	const preserveFailure = !completed && process.env.SYNTH_PHASE4_PRESERVE_FAILURE === "1";
	if (secondHarness && !secondShutdown) {
		await emitSessionShutdownEvent(secondHarness.session.extensionRunner, {
			type: "session_shutdown",
			reason: "reload",
		}).catch(() => undefined);
	}
	if (!preserveFailure) secondHarness?.cleanup();
	if (firstHarness && !firstShutdown) {
		await emitSessionShutdownEvent(firstHarness.session.extensionRunner, {
			type: "session_shutdown",
			reason: "reload",
		}).catch(() => undefined);
	}
	if (!preserveFailure) firstHarness?.cleanup();
	if (originalFixture === undefined) delete process.env.SYNTH_MEMORY_FIXTURE;
	else process.env.SYNTH_MEMORY_FIXTURE = originalFixture;
	if (originalEmbedCache === undefined) delete process.env.SYNTH_FASTEMBED_CACHE_DIR;
	else process.env.SYNTH_FASTEMBED_CACHE_DIR = originalEmbedCache;
	if (preserveFailure) {
		console.error(
			`synth-demo: preserved Phase 4 failure workspace demo=${demoDir} first=${firstHarness?.tempDir ?? "cleaned"} second=${secondHarness?.tempDir ?? "absent"}`,
		);
	} else {
		rmSync(demoDir, { recursive: true, force: true });
	}
}
