import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { registerFauxProvider } from "@earendil-works/pi-ai";
import {
	AuthStorage,
	createAgentSession,
	SessionManager,
} from "../packages/coding-agent/src/index.js";
import { emitSessionShutdownEvent } from "../packages/coding-agent/src/core/extensions/runner.js";

interface CompileReceipt {
	requestId: string;
	status: string;
	error?: string;
}

function isCompileReceipt(value: unknown): value is CompileReceipt {
	return (
		typeof value === "object" &&
		value !== null &&
		"requestId" in value &&
		typeof value.requestId === "string" &&
		"status" in value &&
		typeof value.status === "string"
	);
}

function compileReceipts(sessionManager: SessionManager): CompileReceipt[] {
	return sessionManager.getEntries().flatMap((entry) => {
		if (
			entry.type !== "custom" ||
			entry.customType !== "synth.skill.compile" ||
			!isCompileReceipt(entry.data)
		) {
			return [];
		}
		return [entry.data];
	});
}

async function waitForFailure(
	sessionManager: SessionManager,
	requestId: string,
): Promise<CompileReceipt> {
	for (let attempt = 0; attempt < 100; attempt++) {
		const receipt = compileReceipts(sessionManager).find(
			(item) => item.requestId === requestId && item.status === "failed",
		);
		if (receipt) return receipt;
		await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 25));
	}
	throw new Error("timed out waiting for the recovered compiler failure receipt");
}

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const temporaryRoot = mkdtempSync(join(tmpdir(), "synth-phase8-compiler-crash-"));
const sessionDir = join(temporaryRoot, "sessions");
const agentDir = join(temporaryRoot, "agent");
const requestId = randomUUID();
const jobDirectory = join(repositoryRoot, ".synth", "compiler-jobs");
const jobOutput = join(jobDirectory, `${requestId}.json`);
const jobError = join(jobDirectory, `${requestId}.stderr`);
const originalOffline = process.env.PI_OFFLINE;
const originalUvOffline = process.env.UV_OFFLINE;
let session: Awaited<ReturnType<typeof createAgentSession>>["session"] | undefined;
let shutdown = false;
const faux = registerFauxProvider();

try {
	process.env.PI_OFFLINE = "1";
	process.env.UV_OFFLINE = "1";
	mkdirSync(sessionDir, { recursive: true });
	mkdirSync(agentDir, { recursive: true });
	mkdirSync(jobDirectory, { recursive: true });
	writeFileSync(jobOutput, "");
	writeFileSync(jobError, "fixture compiler crashed before emitting its JSON outcome\n");

	const initialManager = SessionManager.create(repositoryRoot, sessionDir);
	const sessionFile = initialManager.getSessionFile();
	assert.ok(sessionFile);
	initialManager.appendCustomEntry("synth.skill.compile", {
		requestId,
		workflow:
			"Normalize captured command output and compare stable results before reporting evidence.",
		nameHint: "phase8-crash-recovery",
		reason: "Phase 8 detached compiler recovery proof",
		source: "tool",
		sessionFile,
		sessionId: initialManager.getSessionId(),
		timestamp: Date.now(),
		status: "started",
		pid: 2_147_483_647,
		jobOutput,
		jobError,
	});
	initialManager.flushNow();

	const resumedManager = SessionManager.open(sessionFile, sessionDir);
	const authStorage = AuthStorage.inMemory();
	authStorage.setRuntimeApiKey(faux.getModel().provider, "faux-key");
	const created = await createAgentSession({
		cwd: repositoryRoot,
		agentDir,
		model: faux.getModel(),
		authStorage,
		sessionManager: resumedManager,
		tools: [],
		serializedRefine: true,
	});
	session = created.session;
	assert.deepEqual(created.extensionsResult.errors, []);
	const extensionErrors: string[] = [];
	await session.bindExtensions({
		onError: (error) => extensionErrors.push(`${error.event}: ${String(error.error)}`),
	});

	const failed = await waitForFailure(resumedManager, requestId);
	assert.match(failed.error ?? "", /crashed before emitting its JSON outcome/);
	assert.deepEqual(extensionErrors, []);
	const persisted = readFileSync(sessionFile, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as Record<string, unknown>)
		.filter(
			(entry) =>
				entry.type === "custom" &&
				entry.customType === "synth.skill.compile" &&
				(entry.data as Record<string, unknown> | undefined)?.requestId === requestId,
		);
	assert.deepEqual(
		persisted.map((entry) => (entry.data as Record<string, unknown>).status),
		["started", "failed"],
	);

	await emitSessionShutdownEvent(session.extensionRunner, {
		type: "session_shutdown",
		reason: "reload",
	});
	shutdown = true;
	console.log(
		"synth-demo: Phase 8 detached compiler recovery PASS started=1 failed_receipt=1 agent=preserved",
	);
} finally {
	if (session) {
		if (!shutdown) {
			await emitSessionShutdownEvent(session.extensionRunner, {
				type: "session_shutdown",
				reason: "reload",
			}).catch(() => undefined);
		}
		await session.disposeAsync();
	}
	faux.unregister();
	if (originalOffline === undefined) delete process.env.PI_OFFLINE;
	else process.env.PI_OFFLINE = originalOffline;
	if (originalUvOffline === undefined) delete process.env.UV_OFFLINE;
	else process.env.UV_OFFLINE = originalUvOffline;
	rmSync(jobOutput, { force: true });
	rmSync(jobError, { force: true });
	if (existsSync(temporaryRoot)) rmSync(temporaryRoot, { recursive: true, force: true });
}
