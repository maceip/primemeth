import assert from "node:assert/strict";
import { randomUUID, createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
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
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import {
	fauxAssistantMessage,
	fauxToolCall,
	registerFauxProvider,
	type Context,
	type FauxProviderRegistration,
	type FauxResponseStep,
} from "@earendil-works/pi-ai";
import { Type } from "typebox";
import {
	AuthStorage,
	createAgentSession,
	type AgentSession,
	type ExtensionCommandContextActions,
	type ToolCallEvent,
	type ToolDefinition,
	SessionManager,
} from "../packages/coding-agent/src/index.js";
import { emitSessionShutdownEvent } from "../packages/coding-agent/src/core/extensions/runner.js";
import {
	LESSONS_HEADING,
	parseMemoryItems,
	PROBLEM_FRAMING_HEADING,
	renderProblemFraming,
} from "../.prime/agent/extensions/synth/memory.js";
import { controlPlaneBlockReason } from "../.prime/agent/extensions/synth/guard.js";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const demoDir = mkdtempSync(join(tmpdir(), "synth-phase7-demo-"));
const dataPath = join(repositoryRoot, ".synth", "phase7-data.txt");
const promotedPath = join(repositoryRoot, ".prime", "agent", "skills", "synth-line-normalizer");
const quarantineRoot = join(repositoryRoot, ".synth", "quarantine");
const demotedRoot = join(repositoryRoot, ".synth", "demoted", "synth-line-normalizer");
const auditPath = join(repositoryRoot, ".synth", "audit", "skills.jsonl");
const compilerFixture = join(repositoryRoot, "tests", "skill_compiler_fixtures", "line-normalizer.json");
const controlPlanePath = join(repositoryRoot, ".prime", "agent", "extensions", "synth", "index.ts");
const originalFixture = process.env.SYNTH_SKILL_FIXTURE;
const originalOffline = process.env.PI_OFFLINE;
const originalUvOffline = process.env.UV_OFFLINE;

interface DemoSession {
	session: AgentSession;
	sessionManager: SessionManager;
	faux: FauxProviderRegistration;
	extensionErrors: string[];
	shutdown: boolean;
}

interface CompileReceipt {
	requestId: string;
	status: "scheduled" | "started" | "promoted" | "rejected" | "failed";
	name?: string;
	contentHash?: string;
}

interface CompileCliOutcome {
	requestId: string;
	sessionId: string;
	name: string | null;
	status: "promoted" | "rejected";
	contentHash: string | null;
	destination: string | null;
	gate: {
		passed: boolean;
		checks: Array<{ name: string; status: string; evidence: string[] }>;
	};
	workflow: string;
}

interface CommandResult {
	status: number;
	stdout: string;
	stderr: string;
}

interface MetaDemoResult {
	firstAction: string;
	secondAction: string;
	modelCalls: number;
	successModelCalls: number;
	successOutcomeCount: number;
	successfulMemoryId: number;
	contrastedSuccessfulMemoryId: number;
	metaCount: number;
	item: unknown;
}

function messageText(message: AgentMessage): string {
	if (!("content" in message)) return "";
	if (typeof message.content === "string") return message.content;
	return message.content
		.flatMap((part) => (part.type === "text" ? [part.text] : []))
		.join("\n");
}

function commandMessages(session: AgentSession): string[] {
	return session.messages.flatMap((message) => {
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

function hashFile(path: string): string {
	return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function isCompileReceipt(value: unknown): value is CompileReceipt {
	return (
		typeof value === "object" &&
		value !== null &&
		"requestId" in value &&
		typeof value.requestId === "string" &&
		"status" in value &&
		["scheduled", "started", "promoted", "rejected", "failed"].includes(String(value.status))
	);
}

function latestCompileReceipt(sessionManager: SessionManager): CompileReceipt | undefined {
	let result: CompileReceipt | undefined;
	for (const entry of sessionManager.getEntries()) {
		if (entry.type !== "custom" || entry.customType !== "synth.skill.compile") continue;
		if (isCompileReceipt(entry.data)) result = entry.data;
	}
	return result;
}

async function waitFor<T>(
	read: () => T | undefined,
	label: string,
	maxAttempts = 1_900,
): Promise<T> {
	for (let attempt = 0; attempt < maxAttempts; attempt++) {
		const value = read();
		if (value !== undefined) return value;
		await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 100));
	}
	throw new Error(`timed out waiting for ${label}`);
}

function run(
	command: string,
	args: readonly string[],
	options: { cwd?: string; expectedStatus?: number } = {},
): CommandResult {
	const result = spawnSync(command, args, {
		cwd: options.cwd ?? repositoryRoot,
		encoding: "utf8",
		env: { ...process.env, PI_OFFLINE: "1", UV_OFFLINE: "1", UV_NO_PROGRESS: "1" },
		maxBuffer: 8 * 1024 * 1024,
	});
	if (result.error) throw result.error;
	const status = result.status ?? -1;
	const expectedStatus = options.expectedStatus ?? 0;
	if (status !== expectedStatus) {
		throw new Error(
			`${command} ${args.join(" ")} exited ${status}, expected ${expectedStatus}: ${result.stderr || result.stdout}`,
		);
	}
	return { status, stdout: result.stdout, stderr: result.stderr };
}

function synthCompile(
	sessionPath: string,
	requestId: string,
	fixturePath: string,
	options: { workflow?: string; expectedStatus: number },
): CommandResult {
	return run(
		"uv",
		[
			"run",
			"--project",
			repositoryRoot,
			"--no-sync",
			"synth",
			"compile",
			"--from",
			sessionPath,
			"--request-id",
			requestId,
			"--actor",
			"phase7-demo",
			"--reason",
			"Deterministic Phase 7 acceptance proof",
			...(options.workflow === undefined ? [] : ["--workflow", options.workflow]),
			"--fixture",
			fixturePath,
		],
		{ expectedStatus: options.expectedStatus },
	);
}

function cloneSession(source: string, destination: string, sessionId: string, cwd = repositoryRoot): void {
	const lines = readFileSync(source, "utf8").trim().split("\n");
	const header = JSON.parse(lines[0] ?? "null") as Record<string, unknown>;
	header.id = sessionId;
	header.cwd = cwd;
	writeFileSync(destination, `${[JSON.stringify(header), ...lines.slice(1)].join("\n")}\n`);
}

function readAudit(path: string): Array<Record<string, unknown>> {
	if (!existsSync(path)) return [];
	return readFileSync(path, "utf8")
		.split("\n")
		.filter(Boolean)
		.map((line) => JSON.parse(line) as Record<string, unknown>);
}

async function createDemoSession(
	label: string,
	responses: FauxResponseStep[],
	customTools: ToolDefinition[] = [],
	tools: string[] = ["ipython", "compile_skill"],
): Promise<DemoSession> {
	const faux = registerFauxProvider();
	faux.setResponses(responses);
	const authStorage = AuthStorage.inMemory();
	authStorage.setRuntimeApiKey(faux.getModel().provider, "faux-key");
	const sessionDir = join(demoDir, label, "sessions");
	const agentDir = join(demoDir, "agent");
	mkdirSync(sessionDir, { recursive: true });
	mkdirSync(agentDir, { recursive: true });
	const sessionManager = SessionManager.create(repositoryRoot, sessionDir);
	const { session, extensionsResult } = await createAgentSession({
		cwd: repositoryRoot,
		agentDir,
		model: faux.getModel(),
		authStorage,
		sessionManager,
		customTools,
		tools,
		serializedRefine: true,
	});
	assert.equal(extensionsResult.errors.length, 0, JSON.stringify(extensionsResult.errors));
	const extensionErrors: string[] = [];
	const actions: ExtensionCommandContextActions = {
		waitForIdle: () => session.waitForIdle(),
		newSession: async () => ({ cancelled: true }),
		fork: async () => ({ cancelled: true }),
		navigateTree: async () => ({ cancelled: true }),
		switchSession: async () => ({ cancelled: true }),
		reload: () => session.reload(),
	};
	await session.bindExtensions({
		commandContextActions: actions,
		onError: (error) => extensionErrors.push(`${error.event}: ${String(error.error)}`),
	});
	return { session, sessionManager, faux, extensionErrors, shutdown: false };
}

async function shutdownDemoSession(demo: DemoSession): Promise<void> {
	if (demo.shutdown) return;
	await emitSessionShutdownEvent(demo.session.extensionRunner, {
		type: "session_shutdown",
		reason: "quit",
	}).catch(() => undefined);
	demo.shutdown = true;
	await demo.session.disposeAsync();
	demo.faux.unregister();
}

if (existsSync(promotedPath)) {
	throw new Error(`Phase 7 demo requires no active ${promotedPath}; demote the prior compiler artifact first`);
}
for (const name of [
	"synth-line-normalizer",
	"assertion-free-skill",
	"network-skill",
	"outside-write-skill",
	"refine",
	"self-modifying-skill",
]) {
	if (existsSync(join(quarantineRoot, name))) {
		throw new Error(`Phase 7 demo requires an empty fixture quarantine path: ${name}`);
	}
}

process.env.SYNTH_SKILL_FIXTURE = compilerFixture;
process.env.PI_OFFLINE = "1";
process.env.UV_OFFLINE = "1";
mkdirSync(dirname(dataPath), { recursive: true });
writeFileSync(dataPath, "before\n");
const controlHashBefore = hashFile(controlPlanePath);

let editExecutions = 0;
let bashExecutions = 0;
const editParameters = Type.Object({
	path: Type.String(),
	oldText: Type.String(),
	newText: Type.String(),
});
const editTool: ToolDefinition<typeof editParameters> = {
	name: "edit",
	label: "Phase 7 edit fixture",
	description: "Apply one deterministic data-plane replacement",
	parameters: editParameters,
	execute: async (_toolCallId, params) => {
		editExecutions++;
		assert.equal(resolve(repositoryRoot, params.path), dataPath);
		const current = readFileSync(dataPath, "utf8");
		assert.ok(current.includes(params.oldText));
		writeFileSync(dataPath, current.replace(params.oldText, params.newText));
		return { content: [{ type: "text", text: "data-plane edit applied" }], details: {} };
	},
};
const bashParameters = Type.Object({ command: Type.String() });
const bashTool: ToolDefinition<typeof bashParameters> = {
	name: "bash",
	label: "Phase 7 bash fixture",
	description: "Read deterministic local data-plane evidence",
	parameters: bashParameters,
	execute: async () => {
		bashExecutions++;
		return {
			content: [{ type: "text", text: `bash-evidence-${bashExecutions}:${readFileSync(dataPath, "utf8").trim()}` }],
			details: { execution: bashExecutions },
		};
	},
};

let firstSession: DemoSession | undefined;
let secondSession: DemoSession | undefined;
let completed = false;

try {
	const taskWorkspace = join(repositoryRoot, ".synth", "guard-fixture");
	const taskWrite: ToolCallEvent = {
		type: "tool_call",
		toolCallId: "task-write",
		toolName: "ipython",
		input: {
			code: `from pathlib import Path\nPath(${JSON.stringify(join(taskWorkspace, "slug.py"))}).write_text("fixed")`,
		},
	};
	assert.equal(controlPlaneBlockReason(taskWrite, taskWorkspace), undefined);
	const protectedWrite: ToolCallEvent = {
		...taskWrite,
		toolCallId: "protected-write",
		input: {
			code: `from pathlib import Path\nPath(${JSON.stringify(join(taskWorkspace, "synth", "model.py"))}).write_text("unsafe")`,
		},
	};
	assert.match(controlPlaneBlockReason(protectedWrite, taskWorkspace) ?? "", /control plane/);

	firstSession = await createDemoSession(
		"compiler-source",
		[
			fauxAssistantMessage(
				fauxToolCall("edit", {
					path: ".prime/agent/extensions/synth/index.ts",
					oldText: "before",
					newText: "after",
				}),
				{ stopReason: "toolUse" },
			),
			fauxAssistantMessage(
				fauxToolCall("edit", {
					path: ".synth/phase7-data.txt",
					oldText: "before",
					newText: "after",
				}),
				{ stopReason: "toolUse" },
			),
			fauxAssistantMessage(fauxToolCall("bash", { command: "read data evidence one" }), {
				stopReason: "toolUse",
			}),
			fauxAssistantMessage(fauxToolCall("ipython", { code: "1 + 1" }), { stopReason: "toolUse" }),
			fauxAssistantMessage(fauxToolCall("bash", { command: "read data evidence two" }), {
				stopReason: "toolUse",
			}),
			fauxAssistantMessage(fauxToolCall("ipython", { code: "2 + 2" }), { stopReason: "toolUse" }),
			fauxAssistantMessage(
				fauxToolCall("compile_skill", {
					workflow:
						"Normalize captured command-output lines, remove empty lines, then compare stable results before reporting verification evidence.",
					nameHint: "synth-line-normalizer",
					reason: "This normalized-output comparison sequence recurred across verification steps.",
				}),
				{ stopReason: "toolUse" },
			),
			fauxAssistantMessage("Compilation is scheduled pending deterministic gate evidence."),
		],
		[editTool, bashTool],
		["ipython", "bash", "edit", "compile_skill"],
	);
	assert.ok(firstSession.session.getActiveToolNames().includes("compile_skill"));
	assert.ok(firstSession.session.resourceLoader.getSkills().skills.some((skill) => skill.name === "synth-compile"));
	await firstSession.session.prompt("Exercise the data-plane guard and compile the repeated verified workflow");
	await firstSession.session.waitForHeadlessIdle();
	assert.equal(firstSession.faux.getPendingResponseCount(), 0);
	assert.deepEqual([editExecutions, bashExecutions], [1, 2]);
	assert.equal(readFileSync(dataPath, "utf8"), "after\n");
	assert.equal(hashFile(controlPlanePath), controlHashBefore);
	const toolResults = firstSession.session.messages.filter((message) => message.role === "toolResult");
	assert.ok(
		toolResults.some(
			(message) =>
				message.toolName === "edit" &&
				messageText(message).includes("control-plane edits are offline-only"),
		),
	);
	assert.ok(
		toolResults.some(
			(message) =>
				message.toolName === "compile_skill" &&
				messageText(message).includes("Synth skill compilation scheduled ("),
		),
	);

	const terminal = await waitFor(() => {
		const receipt = latestCompileReceipt(firstSession!.sessionManager);
		return receipt && ["promoted", "rejected", "failed"].includes(receipt.status) ? receipt : undefined;
	}, "terminal compiler receipt");
	assert.deepEqual(
		{ status: terminal.status, name: terminal.name, hash: terminal.contentHash?.startsWith("sha256:") },
		{ status: "promoted", name: "synth-line-normalizer", hash: true },
	);
	assert.ok(existsSync(promotedPath));
	await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 100));
	await firstSession.session.prompt("/synth reload-skills");
	await firstSession.session.waitForHeadlessIdle();
	assert.ok(firstSession.session.resourceLoader.getSkills().skills.some((skill) => skill.name === "synth-line-normalizer"));
	assert.ok(
		firstSession.session.systemPrompt.includes("<name>synth-line-normalizer</name>") &&
			firstSession.session.systemPrompt.includes("<python_import>synth_line_normalizer</python_import>"),
	);
	assert.ok(
		commandMessages(firstSession.session).some(
			(message) =>
				message.includes("Synth promoted skill synth-line-normalizer") &&
				message.includes("Run /synth reload-skills"),
		),
	);
	assert.ok(commandMessages(firstSession.session).includes("Reloading Synth skills and kernel provisioning..."));
	assert.deepEqual(firstSession.extensionErrors, []);

	const sourceSessionPath = firstSession.sessionManager.getSessionFile();
	assert.ok(sourceSessionPath);
	const sourceSessionId = firstSession.sessionManager.getSessionId();
	const telemetryPath = join(repositoryRoot, ".synth", "telemetry", `${sourceSessionId}.jsonl`);
	const telemetry = readFileSync(telemetryPath, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as Record<string, unknown>);
	assert.ok(
		telemetry.some(
			(event) =>
				event.kind === "tool" &&
				event.errorClass === "ControlPlaneWriteBlocked" &&
				JSON.stringify(event.tool).includes('"name":"edit"'),
		),
	);
	assert.ok(
		telemetry.some(
			(event) =>
				event.kind === "tool" &&
				event.errorClass === undefined &&
				JSON.stringify(event.tool).includes('"name":"edit"'),
		),
	);

	const miningRequestId = randomUUID();
	const mining = synthCompile(sourceSessionPath, miningRequestId, compilerFixture, {
		expectedStatus: 2,
	});
	assert.match(mining.stderr, /skill destination already exists/);
	const capRequestId = randomUUID();
	const capped = synthCompile(sourceSessionPath, capRequestId, compilerFixture, {
		workflow: "This third request must be rejected before generation because the per-session admission cap is exhausted.",
		expectedStatus: 2,
	});
	assert.match(capped.stderr, /cap is exhausted/);
	const sourceAudit = readAudit(auditPath).filter((record) => record.sessionId === sourceSessionId);
	const minedStart = sourceAudit.find(
		(record) => record.requestId === miningRequestId && record.action === "compile_started",
	);
	assert.match(String(minedStart?.workflow), /Recurring tool sequence \(2 occurrences\): bash -> ipython/);
	assert.ok(
		sourceAudit.some(
			(record) => record.requestId === capRequestId && record.action === "compile_cap_rejected",
		),
	);

	const badFixtures: Array<[string, string]> = [
		["assertion-free-skill", "assertion-free test file"],
		["network-skill", "network"],
		["outside-write-skill", "control_plane"],
		["refine", "bundled skill"],
		["self-modifying-skill", "process_execution"],
	];
	for (const [fixtureName, expectedEvidence] of badFixtures) {
		const sessionPath = join(demoDir, `${fixtureName}.jsonl`);
		cloneSession(sourceSessionPath, sessionPath, `phase7-${fixtureName}-${randomUUID()}`);
		const result = synthCompile(
			sessionPath,
			randomUUID(),
			join(repositoryRoot, "tests", "skill_gate_fixtures", fixtureName),
			{
				workflow: "Inspect the repeated local evidence, apply the reusable transformation, and verify the resulting behavior.",
				expectedStatus: 1,
			},
		);
		const outcome = JSON.parse(result.stdout) as CompileCliOutcome;
		assert.equal(outcome.status, "rejected");
		assert.equal(outcome.gate.passed, false);
		assert.ok(JSON.stringify(outcome.gate).includes(expectedEvidence));
		assert.ok(existsSync(join(quarantineRoot, fixtureName)));
	}

	const killWorkspace = join(demoDir, "kill-switch-workspace");
	mkdirSync(killWorkspace, { recursive: true });
	writeFileSync(
		join(killWorkspace, "synth.json"),
		`${JSON.stringify(
			{
				telemetry: { dir: ".synth/telemetry" },
				skillCompiler: {
					enabled: true,
					maxPerSession: 2,
					quarantine: ".synth/quarantine",
				},
				killSwitch: { all: true },
			},
			null,
			2,
		)}\n`,
	);
	const killSessionPath = join(demoDir, "kill-switch.jsonl");
	const killSessionId = `phase7-kill-${randomUUID()}`;
	cloneSession(sourceSessionPath, killSessionPath, killSessionId, killWorkspace);
	const killResult = synthCompile(killSessionPath, randomUUID(), compilerFixture, {
		workflow: "This workflow must never be generated because the global kill switch is active.",
		expectedStatus: 2,
	});
	assert.match(killResult.stderr, /disabled/);
	assert.equal(existsSync(join(killWorkspace, ".synth", "quarantine")), false);
	assert.ok(
		readAudit(join(killWorkspace, ".synth", "audit", "skills.jsonl")).some(
			(record) => record.sessionId === killSessionId && record.action === "compile_disabled",
		),
	);

	const metaCommand = run(
		"uv",
		[
			"run",
			"--project",
			repositoryRoot,
			"--no-sync",
			"python",
			join(repositoryRoot, "scripts", "synth-phase7-meta-demo.py"),
		],
	);
	const meta = JSON.parse(metaCommand.stdout) as MetaDemoResult;
	assert.deepEqual(
		{
			first: meta.firstAction,
			second: meta.secondAction,
			modelCalls: meta.modelCalls,
			successModelCalls: meta.successModelCalls,
			successOutcomes: meta.successOutcomeCount,
			selected: meta.contrastedSuccessfulMemoryId === meta.successfulMemoryId,
			count: meta.metaCount,
		},
		{
			first: "inserted",
			second: "skipped",
			modelCalls: 2,
			successModelCalls: 0,
			successOutcomes: 0,
			selected: true,
			count: 1,
		},
	);
	const framing = renderProblemFraming(parseMemoryItems({ items: [meta.item] }, true));
	assert.ok(
		framing?.startsWith(PROBLEM_FRAMING_HEADING) &&
			!framing.includes(LESSONS_HEADING) &&
			framing.split(PROBLEM_FRAMING_HEADING).length === 2,
	);

	let invokedSystemPrompt = "";
	let invokedToolResults = "";
	secondSession = await createDemoSession("promoted-invocation", [
		(context: Context) => {
			invokedSystemPrompt = context.systemPrompt ?? "";
			return fauxAssistantMessage(
				fauxToolCall("ipython", {
					code: 'synth_line_normalizer.run(["  first  ", "  ", "second"])',
				}),
				{ stopReason: "toolUse" },
			);
		},
		(context: Context) => {
			invokedToolResults = context.messages
				.filter((message) => message.role === "toolResult")
				.flatMap((message) => message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])))
				.join("\n");
			return fauxAssistantMessage("The promoted skill returned normalized lines.");
		},
	]);
	assert.ok(secondSession.session.resourceLoader.getSkills().skills.some((skill) => skill.name === "synth-line-normalizer"));
	await secondSession.session.prompt("Invoke the promoted line-normalization skill on first, blank, and second lines");
	await secondSession.session.waitForHeadlessIdle();
	assert.ok(
		invokedSystemPrompt.includes("<available_skills>") &&
			invokedSystemPrompt.includes("<name>synth-line-normalizer</name>") &&
			invokedSystemPrompt.includes("<python_import>synth_line_normalizer</python_import>"),
	);
	assert.ok(invokedToolResults.includes("['first', 'second']"));
	const invocationResult = secondSession.session.messages.find(
		(message) => message.role === "toolResult" && message.toolName === "ipython",
	);
	assert.ok(invocationResult && messageText(invocationResult).includes("['first', 'second']"));
	assert.deepEqual(secondSession.extensionErrors, []);

	await secondSession.session.prompt("/synth demote synth-line-normalizer");
	await secondSession.session.waitForHeadlessIdle();
	assert.equal(existsSync(promotedPath), false);
	assert.ok(existsSync(demotedRoot));
	assert.ok(
		!secondSession.session.resourceLoader.getSkills().skills.some(
			(skill) => skill.name === "synth-line-normalizer",
		),
	);
	assert.ok(readAudit(auditPath).some((record) => record.name === "synth-line-normalizer" && record.action === "demoted"));
	assert.equal(hashFile(controlPlanePath), controlHashBefore);

	console.log(
		"synth-demo: Phase 7 dynamic compiler PASS promoted=1 invoked=1 bad_rejected=5 cap=2 kill_switch=blocked control_guard=blocked data_plane=written meta_framing=1 demoted=1",
	);
	completed = true;
} finally {
	if (secondSession) await shutdownDemoSession(secondSession);
	if (firstSession) await shutdownDemoSession(firstSession);
	if (originalFixture === undefined) delete process.env.SYNTH_SKILL_FIXTURE;
	else process.env.SYNTH_SKILL_FIXTURE = originalFixture;
	if (originalOffline === undefined) delete process.env.PI_OFFLINE;
	else process.env.PI_OFFLINE = originalOffline;
	if (originalUvOffline === undefined) delete process.env.UV_OFFLINE;
	else process.env.UV_OFFLINE = originalUvOffline;
	if (completed) {
		rmSync(dataPath, { force: true });
		for (const name of [
			"synth-line-normalizer",
			"assertion-free-skill",
			"network-skill",
			"outside-write-skill",
			"refine",
			"self-modifying-skill",
		]) {
			rmSync(join(quarantineRoot, name), { recursive: true, force: true });
		}
		rmSync(demotedRoot, { recursive: true, force: true });
		rmSync(demoDir, { recursive: true, force: true });
	} else {
		console.error(`synth-demo: preserved Phase 7 failure artifacts under ${demoDir}`);
	}
}
