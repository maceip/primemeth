import { randomUUID, timingSafeEqual } from "node:crypto";
import { spawn, type ChildProcess } from "node:child_process";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { Usage } from "@earendil-works/pi-ai";
import {
	convertToLlm,
	serializeConversation,
	type ExtensionAPI,
	type ExtensionCommandContext,
	type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { BridgeClient } from "./bridge.js";
import {
	assistantStalled,
	BATS_CONTINUATION_NUDGE,
	BATS_ELISION_MARKER,
	BATS_WRAP_UP_INSTRUCTION,
	batchBudgetUsedFromEnvironment,
	BudgetTracker,
	elideOldToolResponses,
	formatBudgetBlock,
	formatBudgetStatusLine,
	injectBatsSystemPrompt,
	shouldSendContinuationNudge,
} from "./budget.js";
import {
	buildCompileMarker,
	compilerArguments,
	CompileSkillParameters,
	createCompileRequest,
	parseCompileMarker,
	parseCompilerOutcome,
	parsePersistedCompile,
	persistCompile,
	type CompileMarker,
	type CompileRequest,
	type PersistedCompile,
} from "./compiler.js";
import { loadSynthConfig, resolveDataPlanePath, type SynthConfig, type SynthConfigLoadResult } from "./config.js";
import { controlPlaneBlockReason, managedControlPlaneWarning } from "./guard.js";
import {
	LESSONS_HEADING,
	parseMemoryItems,
	parsePersistedMemoryRetrieval,
	PROBLEM_FRAMING_HEADING,
	REASONING_BANK_TASK_DESCRIPTION,
	renderLessons,
	renderMemoryList,
	renderMemorySearch,
	renderProblemFraming,
} from "./memory.js";
import {
	type BudgetSnapshot,
	copyUsage,
	digestValue,
	parseTerminalOutcomeReceipt,
	TelemetryWriter,
	type TerminalOutcome,
	type TelemetryEventInput,
} from "./telemetry.js";
import {
	appendActivePlan,
	assistantText,
	buildCompactionPrompt,
	buildVerificationPrompt,
	condenseTrajectory,
	extractLastPlan,
	finalAssistant,
	formatVerifierGuidance,
	parsePersistedVerification,
	type PersistedVerification,
	VerifierClient,
} from "./verifier.js";

const BATCH_OUTCOME_TOKEN = process.env.SYNTH_BATCH_OUTCOME_TOKEN;
delete process.env.SYNTH_BATCH_OUTCOME_TOKEN;

interface TranscriptRef {
	sessionFile: string;
	entryId: string | null;
}

interface ToolStart {
	startedAt: number;
	argsDigest: string;
	entryId: string | null;
}

interface ProviderExchange {
	turnIndex: number;
	requestId: string;
	startedAt: number;
	payloadDigest: string;
	provider: string | null;
	model: string | null;
	status?: number;
}

interface CompilerJobPaths {
	output: string;
	error: string;
}

interface CompilerProcessResult {
	code: number | null;
	signal: NodeJS.Signals | null;
	error?: Error;
}

interface DetachedCompilerJob {
	pid?: number;
	paths: CompilerJobPaths;
	completion: Promise<CompilerProcessResult>;
}

interface SessionRuntime {
	configLoad: SynthConfigLoadResult;
	telemetry?: TelemetryWriter;
	bridge?: BridgeClient;
	budget?: BudgetTracker;
	warnings: Set<string>;
	turnStarts: Map<number, number>;
	toolStarts: Map<string, ToolStart>;
	providers: Map<number, ProviderExchange>;
	initialBudgetInjected: boolean;
	allowedToolCallsThisTurn: number;
	noToolCount: number;
	nudgePending: boolean;
	taskQuery?: string;
	memoryLookup?: Promise<void>;
	memorySection?: string;
	memoryItemIds: number[];
	framingSection?: string;
	framingItemIds: number[];
	memoryPersisted: boolean;
	distillEnqueued: boolean;
	verifier?: VerifierClient;
	verifiedEntryIds: Set<string>;
	verificationCycles: number;
	verificationInFlight: boolean;
	latestVerification?: PersistedVerification;
	compileRequests: Map<string, PersistedCompile>;
	activeCompileRequest?: CompileRequest;
	compileInFlight?: Promise<void>;
	batchOutcome?: TerminalOutcome;
	outcomePersisted: boolean;
	lastToolError?: TranscriptRef;
	terminalToolError?: TranscriptRef;
	shuttingDown: boolean;
}

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
}

function secureTokenEquals(actual: string, expected: string): boolean {
	const actualBytes = Buffer.from(actual);
	const expectedBytes = Buffer.from(expected);
	return actualBytes.length === expectedBytes.length && timingSafeEqual(actualBytes, expectedBytes);
}

function assistantUsage(message: AgentMessage): Usage | undefined {
	return message.role === "assistant" ? message.usage : undefined;
}

function assistantErrorClass(message: AgentMessage): string | undefined {
	return message.role === "assistant" && message.stopReason === "error" ? "AssistantProviderError" : undefined;
}

function resultErrorClass(result: unknown): string {
	if (result instanceof Error) {
		return result.name;
	}
	if (result && typeof result === "object" && "error" in result && result.error instanceof Error) {
		return result.error.name;
	}
	return "ToolExecutionError";
}

function commandOutput(pi: ExtensionAPI, text: string): void {
	pi.sendMessage(
		{
			customType: "synth.command",
			content: text,
			display: true,
			details: { timestamp: Date.now() },
		},
		{ triggerTurn: false },
	);
}

function formatBudgets(config: SynthConfig, budget?: BudgetTracker): string {
	if (budget) {
		return formatBudgetStatusLine(budget.snapshot());
	}
	const { bash, edit, ipython, subagents, tokens, costUsd } = config.budgets;
	return `configured bash=${bash} edit=${edit} ipython=${ipython} subagents=${subagents} tokens=${tokens} costUsd=${costUsd}`;
}

function createRuntime(cwd: string): SessionRuntime {
	return {
		configLoad: loadSynthConfig(cwd),
		warnings: new Set(),
		turnStarts: new Map(),
		toolStarts: new Map(),
		providers: new Map(),
		initialBudgetInjected: false,
		allowedToolCallsThisTurn: 0,
		noToolCount: 0,
		nudgePending: false,
		memoryItemIds: [],
		framingItemIds: [],
		memoryPersisted: false,
		distillEnqueued: false,
		verifiedEntryIds: new Set(),
		verificationCycles: 0,
		verificationInFlight: false,
		compileRequests: new Map(),
		outcomePersisted: false,
		shuttingDown: false,
	};
}

function contentText(content: unknown): string {
	if (typeof content === "string") {
		return content;
	}
	if (!Array.isArray(content)) {
		return "";
	}
	return content
		.flatMap((item) =>
			item && typeof item === "object" && "type" in item && item.type === "text" && "text" in item
				? [String(item.text)]
				: [],
		)
		.join("\n");
}

function messageContains(message: AgentMessage, text: string): boolean {
	return "content" in message && contentText(message.content).includes(text);
}

function appendBudgetToUser(message: AgentMessage, snapshot: BudgetSnapshot): AgentMessage {
	if (message.role !== "user" || messageContains(message, "<budget>")) {
		return message;
	}
	const block = formatBudgetBlock(snapshot);
	return {
		...message,
		content:
			typeof message.content === "string"
				? `${message.content}${block}`
				: [...message.content, { type: "text", text: block }],
	};
}

function toolBudgetText(snapshot: BudgetSnapshot): string {
	return `${snapshot.fullyExhausted ? `${BATS_WRAP_UP_INSTRUCTION}\n` : ""}${formatBudgetBlock(snapshot)}`;
}

function appendBudgetToToolResult(message: AgentMessage, snapshot: BudgetSnapshot): AgentMessage {
	if (
		message.role !== "toolResult" ||
		messageContains(message, "<budget>") ||
		messageContains(message, BATS_ELISION_MARKER)
	) {
		return message;
	}
	return {
		...message,
		content: [...message.content, { type: "text", text: toolBudgetText(snapshot) }],
	};
}

function transcript(ctx: ExtensionContext, entryId = ctx.sessionManager.getLeafId()): TranscriptRef | undefined {
	const sessionFile = ctx.sessionManager.getSessionFile();
	return sessionFile ? { sessionFile, entryId } : undefined;
}

function messageEntryId(ctx: ExtensionContext, message: AgentMessage): string | null {
	const branch = ctx.sessionManager.getBranch();
	for (let index = branch.length - 1; index >= 0; index--) {
		const entry = branch[index];
		if (entry.type === "message" && entry.message === message) {
			return entry.id;
		}
	}
	return null;
}

function toolCallEntryId(ctx: ExtensionContext, toolCallId: string): string | null {
	const branch = ctx.sessionManager.getBranch();
	for (let index = branch.length - 1; index >= 0; index--) {
		const entry = branch[index];
		if (
			entry.type === "message" &&
			entry.message.role === "assistant" &&
			entry.message.content.some((part) => part.type === "toolCall" && part.id === toolCallId)
		) {
			return entry.id;
		}
	}
	return null;
}

function latestProvider(runtime: SessionRuntime): ProviderExchange | undefined {
	let result: ProviderExchange | undefined;
	for (const provider of runtime.providers.values()) {
		if (provider.status === undefined) {
			result = provider;
		}
	}
	return result;
}

function firstProviderWithoutStatus(runtime: SessionRuntime): ProviderExchange | undefined {
	for (const provider of runtime.providers.values()) {
		if (provider.status === undefined) {
			return provider;
		}
	}
	return undefined;
}

function restorePersistedMemory(runtime: SessionRuntime, ctx: ExtensionContext): void {
	const branch = ctx.sessionManager.getBranch();
	for (let index = branch.length - 1; index >= 0; index--) {
		const entry = branch[index];
		if (entry.type !== "custom" || entry.customType !== "synth.memory.retrieval") {
			continue;
		}
		const persisted = parsePersistedMemoryRetrieval(entry.data);
		if (!persisted) {
			continue;
		}
		runtime.taskQuery = persisted.taskQuery;
		runtime.memorySection = persisted.section;
		runtime.memoryItemIds = persisted.itemIds;
		runtime.framingSection = persisted.framingSection;
		runtime.framingItemIds = persisted.framingItemIds;
		runtime.memoryPersisted = true;
		runtime.memoryLookup = Promise.resolve();
		return;
	}
}

function hasMeaningfulTrajectory(ctx: ExtensionContext): boolean {
	let hasUser = false;
	let hasAssistant = false;
	for (const entry of ctx.sessionManager.getBranch()) {
		if (entry.type !== "message") {
			continue;
		}
		hasUser ||= entry.message.role === "user";
		hasAssistant ||= entry.message.role === "assistant";
	}
	return hasUser && hasAssistant;
}

function cleanTaskPrompt(text: string): string {
	return text
		.replace(/\n?<budget>[\s\S]*?<\/budget>\n?/g, "\n")
		.replace(/\n?\[Budget:[\s\S]*?\| Regime: [A-Z]+\]\n?/g, "\n")
		.trim();
}

function firstTaskFromBranch(ctx: ExtensionContext): string | undefined {
	for (const entry of ctx.sessionManager.getBranch()) {
		if (entry.type !== "message" || entry.message.role !== "user") {
			continue;
		}
		const task = cleanTaskPrompt(contentText(entry.message.content));
		if (task) return task;
	}
	return undefined;
}

function restorePersistedVerifications(runtime: SessionRuntime, ctx: ExtensionContext): void {
	for (const entry of ctx.sessionManager.getBranch()) {
		if (entry.type !== "custom" || entry.customType !== "synth.verifier.decision") {
			continue;
		}
		const persisted = parsePersistedVerification(entry.data);
		if (!persisted) continue;
		runtime.verifiedEntryIds.add(persisted.assistantEntryId);
		runtime.verificationCycles = Math.max(runtime.verificationCycles, persisted.cycle);
		runtime.latestVerification = persisted;
		runtime.taskQuery ??= persisted.task;
	}
}

function restorePersistedCompiles(runtime: SessionRuntime, ctx: ExtensionContext): void {
	for (const entry of ctx.sessionManager.getEntries()) {
		if (entry.type !== "custom" || entry.customType !== "synth.skill.compile") {
			continue;
		}
		const persisted = parsePersistedCompile(entry.data);
		if (persisted) runtime.compileRequests.set(persisted.requestId, persisted);
	}
}

function compileMarkerMatches(existing: PersistedCompile, marker: CompileMarker): boolean {
	return (
		existing.reason === marker.reason &&
		existing.source === marker.source &&
		existing.workflow === marker.workflow &&
		existing.nameHint === marker.nameHint
	);
}

function compileRequestFromPersisted(persisted: PersistedCompile): CompileRequest {
	return {
		requestId: persisted.requestId,
		reason: persisted.reason,
		source: persisted.source,
		sessionFile: persisted.sessionFile,
		sessionId: persisted.sessionId,
		timestamp: persisted.timestamp,
		...(persisted.workflow === undefined ? {} : { workflow: persisted.workflow }),
		...(persisted.nameHint === undefined ? {} : { nameHint: persisted.nameHint }),
	};
}

function compilerJobPaths(cwd: string, requestId: string): CompilerJobPaths {
	const directory = resolveDataPlanePath(cwd, ".synth/compiler-jobs");
	return {
		output: join(directory, `${requestId}.json`),
		error: join(directory, `${requestId}.stderr`),
	};
}

function spawnDetachedCompiler(cwd: string, args: string[], paths: CompilerJobPaths): DetachedCompilerJob {
	mkdirSync(dirname(paths.output), { recursive: true });
	const outputFd = openSync(paths.output, "wx", 0o600);
	let errorFd: number | undefined;
	let child: ChildProcess;
	try {
		errorFd = openSync(paths.error, "wx", 0o600);
		child = spawn("uv", args, {
			cwd,
			detached: true,
			env: { ...process.env, UV_NO_PROGRESS: "1" },
			stdio: ["ignore", outputFd, errorFd],
		});
	} finally {
		closeSync(outputFd);
		if (errorFd !== undefined) closeSync(errorFd);
	}
	const completion = new Promise<CompilerProcessResult>((resolveCompletion) => {
		let settled = false;
		const settle = (result: CompilerProcessResult): void => {
			if (settled) return;
			settled = true;
			resolveCompletion(result);
		};
		child.once("error", (error) => settle({ code: null, signal: null, error }));
		child.once("exit", (code, signal) => settle({ code, signal }));
	});
	child.unref();
	return { ...(child.pid === undefined ? {} : { pid: child.pid }), paths, completion };
}

function processIsRunning(pid: number | undefined): boolean {
	if (pid === undefined) return true;
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
}

async function waitForRecoveredCompilerJob(job: DetachedCompilerJob): Promise<CompilerProcessResult> {
	for (let attempt = 0; attempt < 1_800; attempt++) {
		if (existsSync(job.paths.output)) {
			const output = readFileSync(job.paths.output, "utf8").trim();
			if (output) {
				try {
					parseCompilerOutcome(output);
					return { code: null, signal: null };
				} catch {
					// The detached child may still be flushing its single JSON result.
				}
			}
		}
		if (!processIsRunning(job.pid)) return { code: null, signal: null };
		await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 100));
	}
	return {
		code: null,
		signal: null,
		error: new Error("detached Synth compiler did not finish within 180 seconds"),
	};
}

export default function synthExtension(pi: ExtensionAPI): void {
	const runtimes = new WeakMap<object, SessionRuntime>();

	function runtimeFor(ctx: ExtensionContext): SessionRuntime {
		const existing = runtimes.get(ctx.sessionManager);
		if (existing) {
			return existing;
		}
		const runtime = createRuntime(ctx.cwd);
		runtimes.set(ctx.sessionManager, runtime);
		return runtime;
	}

	function warn(runtime: SessionRuntime, ctx: ExtensionContext, category: string, message: string): void {
		if (runtime.warnings.has(category)) {
			return;
		}
		runtime.warnings.add(category);
		console.error(`synth: ${message}`);
		if (ctx.hasUI) {
			ctx.ui.notify(`Synth: ${message}`, "warning");
		}
	}

	function appendTelemetry(runtime: SessionRuntime, ctx: ExtensionContext, event: TelemetryEventInput): boolean {
		if (!runtime.telemetry) {
			return false;
		}
		try {
			runtime.telemetry.append(event);
			return true;
		} catch (error) {
			runtime.telemetry = undefined;
			warn(runtime, ctx, "telemetry", `telemetry disabled after a write failure: ${errorMessage(error)}`);
			return false;
		}
	}

	function outcomeEvidence(runtime: SessionRuntime, ctx: ExtensionContext): TerminalOutcome["evidence"] | undefined {
		const pointer = transcript(ctx);
		if (pointer) {
			return { kind: "transcript", uri: pointer.sessionFile, entry_id: pointer.entryId };
		}
		if (runtime.telemetry) {
			return { kind: "artifact", uri: runtime.telemetry.path, entry_id: null };
		}
		return undefined;
	}

	function runtimeTerminalOutcome(runtime: SessionRuntime, ctx: ExtensionContext): TerminalOutcome | undefined {
		const evidence = outcomeEvidence(runtime, ctx);
		if (!evidence) return undefined;
		if (runtime.budget?.snapshot().fullyExhausted) {
			return { reward: null, termination_reason: "BUDGET_EXHAUSTED", evidence };
		}
		if (runtime.terminalToolError) {
			return {
				reward: null,
				termination_reason: "TOOL_ERROR",
				evidence: {
					kind: "transcript",
					uri: runtime.terminalToolError.sessionFile,
					entry_id: runtime.terminalToolError.entryId,
				},
			};
		}
		return { reward: null, termination_reason: "UNKNOWN", evidence };
	}

	function managedBatchSession(ctx: ExtensionContext): boolean {
		return (
			process.env.SYNTH_BATCH_RUN_ID !== undefined &&
			process.env.SYNTH_BATCH_TRAJECTORY_INDEX !== undefined &&
			(ctx.sessionManager.getHeader()?.rlmDepth ?? 0) === 0
		);
	}

	function batchTerminalOutcome(runtime: SessionRuntime, ctx: ExtensionContext): TerminalOutcome | undefined {
		const runId = process.env.SYNTH_BATCH_RUN_ID;
		const rawIndex = process.env.SYNTH_BATCH_TRAJECTORY_INDEX;
		if (!runId || !rawIndex || !managedBatchSession(ctx)) return undefined;
		if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(runId)) {
			warn(runtime, ctx, "terminal-outcome", "batch terminal outcome ignored because the run id is unsafe");
			return undefined;
		}
		const trajectoryIndex = Number(rawIndex);
		if (!Number.isSafeInteger(trajectoryIndex) || trajectoryIndex < 1) {
			warn(runtime, ctx, "terminal-outcome", "batch terminal outcome ignored because its trajectory index is invalid");
			return undefined;
		}
		if (runtime.batchOutcome) return runtime.batchOutcome;
		return undefined;
	}

	function persistTerminalOutcome(runtime: SessionRuntime, ctx: ExtensionContext): void {
		if (runtime.outcomePersisted || !runtime.telemetry) return;
		const managedOutcome = batchTerminalOutcome(runtime, ctx);
		if (managedBatchSession(ctx) && !managedOutcome) return;
		const outcome = managedOutcome ?? runtimeTerminalOutcome(runtime, ctx);
		if (!outcome) return;
		const pointer = transcript(ctx);
		if (
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "session.outcome",
				outcome,
				...(pointer ? { transcript: pointer } : {}),
			})
		) {
			runtime.outcomePersisted = true;
		}
	}

	function persistBudget(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		reason: string,
		data: Record<string, unknown> = {},
	): void {
		if (!runtime.budget) {
			return;
		}
		const pointer = transcript(ctx);
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "budget",
			budget: runtime.budget.snapshot(),
			...(pointer ? { transcript: pointer } : {}),
			data: { reason, ...data },
		});
	}

	function persistCompileState(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		request: CompileRequest,
		status: PersistedCompile["status"],
		detail: Partial<PersistedCompile> = {},
	): PersistedCompile {
		const persisted = persistCompile(request, status, detail);
		pi.appendEntry("synth.skill.compile", persisted);
		ctx.sessionManager.flushNow();
		runtime.compileRequests.set(request.requestId, persisted);
		const pointer = transcript(ctx);
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "bridge",
			...(pointer ? { transcript: pointer } : {}),
			...(status === "failed" ? { errorClass: "SkillCompilerFailed" } : {}),
			data: {
				action: `skill.compile.${status}`,
				requestId: request.requestId,
				source: request.source,
				...(detail.name === undefined ? {} : { name: detail.name }),
				...(detail.contentHash === undefined ? {} : { contentHash: detail.contentHash }),
				...(detail.error === undefined ? {} : { error: detail.error }),
			},
		});
		return persisted;
	}

	function admitCompile(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		marker: CompileMarker,
	): { request?: CompileRequest; error?: string } {
		const config = runtime.configLoad.config;
		if (runtime.configLoad.state !== "loaded" || !config) {
			return { error: "Synth configuration is unavailable." };
		}
		if (config.killSwitch.all || !config.skillCompiler.enabled) {
			return { error: "Synth skill compilation is disabled." };
		}
		if (marker.requestId) {
			const existing = runtime.compileRequests.get(marker.requestId);
			if (!existing) return { error: "Synth compile marker references an unknown request." };
			if (existing.status !== "scheduled") {
				return { error: `Synth compile request ${marker.requestId} was already consumed.` };
			}
			if (!compileMarkerMatches(existing, marker)) {
				return { error: "Synth compile marker does not match its persisted request." };
			}
			if (runtime.compileInFlight || runtime.activeCompileRequest) {
				return { error: "Another Synth skill compilation is already active." };
			}
			const otherPending = [...runtime.compileRequests.values()].some(
				(request) =>
					request.requestId !== existing.requestId &&
					(request.status === "scheduled" || request.status === "started"),
			);
			return otherPending
				? { error: "Another Synth skill compilation is already pending." }
				: { request: compileRequestFromPersisted(existing) };
		}
		if (
			runtime.compileInFlight ||
			runtime.activeCompileRequest ||
			[...runtime.compileRequests.values()].some(
				(request) => request.status === "scheduled" || request.status === "started",
			)
		) {
			return { error: "Another Synth skill compilation is already active." };
		}
		if (runtime.compileRequests.size >= config.skillCompiler.maxPerSession) {
			return {
				error: `Synth skill compilation cap reached (${config.skillCompiler.maxPerSession} per session).`,
			};
		}
		const sessionFile = ctx.sessionManager.getSessionFile();
		if (!sessionFile) {
			return { error: "Skill compilation requires a persisted session." };
		}
		const request = createCompileRequest(
			marker,
			sessionFile,
			ctx.sessionManager.getSessionId(),
		);
		try {
			persistCompileState(runtime, ctx, request, "scheduled");
		} catch (error) {
			return { error: `Could not persist the compiler request: ${errorMessage(error)}` };
		}
		return { request };
	}

	function queueCompile(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		marker: CompileMarker,
	): string {
		const admission = admitCompile(runtime, ctx, marker);
		if (!admission.request) return admission.error ?? "Skill compilation was not scheduled.";
		try {
			pi.sendUserMessage(`/refine ${buildCompileMarker(admission.request)}`, { deliverAs: "followUp" });
		} catch (error) {
			persistCompileState(runtime, ctx, admission.request, "failed", {
				error: `Could not enqueue native refinement: ${errorMessage(error)}`,
			});
			return `Synth skill compilation was not scheduled: ${errorMessage(error)}`;
		}
		return `Synth skill compilation scheduled (${admission.request.requestId}). Generation and gating run after the current turn.`;
	}

	function compilerFailure(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		request: CompileRequest,
		error: unknown,
	): void {
		if (runtime.shuttingDown) return;
		const detail = errorMessage(error);
		try {
			persistCompileState(runtime, ctx, request, "failed", { error: detail.slice(0, 2000) });
		} catch {
			// The Python audit and durable job files remain authoritative.
		}
		commandOutput(pi, `Synth skill compilation failed: ${detail}`);
	}

	function finishCompilerJob(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		request: CompileRequest,
		job: DetachedCompilerJob,
		result: CompilerProcessResult,
	): void {
		if (runtime.shuttingDown) return;
		if (result.error) throw result.error;
		if (result.signal !== null) throw new Error(`compiler terminated by signal ${result.signal}`);
		const stdout = existsSync(job.paths.output) ? readFileSync(job.paths.output, "utf8").trim() : "";
		const stderr = existsSync(job.paths.error) ? readFileSync(job.paths.error, "utf8").trim() : "";
		if (!stdout) throw new Error(stderr || `compiler exited ${result.code ?? "without a receipt"}`);
		const outcome = parseCompilerOutcome(stdout);
		if (outcome.requestId !== request.requestId || outcome.sessionId !== request.sessionId) {
			throw new Error("Synth compiler outcome does not match its durable request");
		}
		const expectedCode = outcome.status === "promoted" ? 0 : 1;
		if (result.code !== null && result.code !== expectedCode) {
			throw new Error(
				`Synth compiler returned ${outcome.status} with inconsistent exit code ${result.code}`,
			);
		}
		persistCompileState(runtime, ctx, request, outcome.status, {
			...(outcome.name === null ? {} : { name: outcome.name }),
			...(outcome.contentHash === null ? {} : { contentHash: outcome.contentHash }),
			pid: job.pid,
			jobOutput: job.paths.output,
			jobError: job.paths.error,
		});
		commandOutput(
			pi,
			outcome.status === "promoted"
				? `Synth promoted skill ${outcome.name ?? "unknown"} (${outcome.contentHash ?? "no hash"}). Run /synth reload-skills to provision it now, or start a new session.`
				: `Synth rejected skill ${outcome.name ?? "unknown"}; inspect the quarantine audit for gate evidence.`,
		);
	}

	async function executeCompiler(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		request: CompileRequest,
	): Promise<void> {
		const fixturePath = process.env.SYNTH_SKILL_FIXTURE;
		try {
			const paths = compilerJobPaths(ctx.cwd, request.requestId);
			persistCompileState(runtime, ctx, request, "started", {
				jobOutput: paths.output,
				jobError: paths.error,
			});
			const job = spawnDetachedCompiler(
				ctx.cwd,
				compilerArguments(ctx.cwd, request, fixturePath),
				paths,
			);
			persistCompileState(runtime, ctx, request, "started", {
				...(job.pid === undefined ? {} : { pid: job.pid }),
				jobOutput: paths.output,
				jobError: paths.error,
			});
			finishCompilerJob(runtime, ctx, request, job, await job.completion);
		} catch (error) {
			compilerFailure(runtime, ctx, request, error);
		}
	}

	async function reconcileCompiler(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		persisted: PersistedCompile,
	): Promise<void> {
		const request = compileRequestFromPersisted(persisted);
		const paths = compilerJobPaths(ctx.cwd, request.requestId);
		const job: DetachedCompilerJob = {
			...(persisted.pid === undefined ? {} : { pid: persisted.pid }),
			paths,
			completion: Promise.resolve({ code: null, signal: null }),
		};
		try {
			finishCompilerJob(runtime, ctx, request, job, await waitForRecoveredCompilerJob(job));
		} catch (error) {
			compilerFailure(runtime, ctx, request, error);
		}
	}

	function launchCompiler(runtime: SessionRuntime, ctx: ExtensionContext, request: CompileRequest): void {
		const task = executeCompiler(runtime, ctx, request);
		runtime.compileInFlight = task;
		void task.then(
			() => {
				if (runtime.compileInFlight === task) runtime.compileInFlight = undefined;
			},
			() => {
				if (runtime.compileInFlight === task) runtime.compileInFlight = undefined;
			},
		);
	}

	function resumePersistedCompiler(runtime: SessionRuntime, ctx: ExtensionContext): void {
		const started = [...runtime.compileRequests.values()].find((request) => request.status === "started");
		if (started) {
			const task = reconcileCompiler(runtime, ctx, started);
			runtime.compileInFlight = task;
			void task.then(
				() => {
					if (runtime.compileInFlight === task) runtime.compileInFlight = undefined;
				},
				() => {
					if (runtime.compileInFlight === task) runtime.compileInFlight = undefined;
				},
			);
			return;
		}
		const scheduled = [...runtime.compileRequests.values()].find((request) => request.status === "scheduled");
		if (scheduled && runtime.configLoad.config?.skillCompiler.enabled) {
			pi.sendUserMessage(`/refine ${buildCompileMarker(compileRequestFromPersisted(scheduled))}`, {
				deliverAs: "followUp",
			});
		}
	}

	function bridgeFor(runtime: SessionRuntime, ctx: ExtensionContext): BridgeClient | undefined {
		const config = runtime.configLoad.config;
		if (runtime.configLoad.state !== "loaded" || !config || config.killSwitch.all) {
			return undefined;
		}
		if (!runtime.bridge) {
			runtime.bridge = new BridgeClient(ctx.cwd, (message) => {
				warn(runtime, ctx, "bridge", message);
				const pointer = transcript(ctx);
				appendTelemetry(runtime, ctx, {
					sessionId: ctx.sessionManager.getSessionId(),
					kind: "bridge",
					...(pointer ? { transcript: pointer } : {}),
					errorClass: "BridgeUnavailable",
					data: { message },
				});
			});
		}
		return runtime.bridge;
	}

	function verifierMode(runtime: SessionRuntime): "interactive" | "batch" | undefined {
		const config = runtime.configLoad.config;
		if (!config || config.killSwitch.all || config.verifier.maxCycles === 0) {
			return undefined;
		}
		if (process.env.SYNTH_BATCH_RUN_ID) {
			return config.verifier.batch || process.env.SYNTH_BATCH_FORCE_VERIFIER === "1" ? "batch" : undefined;
		}
		return config.verifier.interactive ? "interactive" : undefined;
	}

	function verifierFor(runtime: SessionRuntime): VerifierClient {
		runtime.verifier ??= new VerifierClient(runtime.configLoad.config?.endpoint);
		return runtime.verifier;
	}

	async function verifyConclusion(
		runtime: SessionRuntime,
		ctx: ExtensionContext,
		messages: readonly AgentMessage[],
	): Promise<boolean> {
		const config = runtime.configLoad.config;
		const mode = verifierMode(runtime);
		const budget = runtime.budget;
		if (!config || !mode || !budget || runtime.verificationInFlight || ctx.hasPendingMessages()) {
			return false;
		}
		if (runtime.verificationCycles >= config.verifier.maxCycles) {
			return false;
		}
		const assistant = finalAssistant(messages);
		if (
			!assistant ||
			assistant.stopReason === "error" ||
			assistant.stopReason === "aborted" ||
			assistant.content.some((part) => part.type === "toolCall")
		) {
			return false;
		}
		const answer = assistantText(assistant);
		const task = runtime.taskQuery ?? firstTaskFromBranch(ctx);
		const assistantEntryId = messageEntryId(ctx, assistant);
		if (!answer || !task || !assistantEntryId || runtime.verifiedEntryIds.has(assistantEntryId)) {
			return false;
		}
		runtime.taskQuery ??= task;
		runtime.verifiedEntryIds.add(assistantEntryId);
		runtime.verificationInFlight = true;
		const cycle = runtime.verificationCycles + 1;
		try {
			const trajectory = condenseTrajectory(ctx.sessionManager.getBranch());
			const result = await verifierFor(runtime).verify(
				buildVerificationPrompt(task, trajectory, answer, budget.snapshot()),
			);
			runtime.verificationCycles = cycle;
			const persisted: PersistedVerification = {
				...result,
				assistantEntryId,
				cycle,
				mode,
				task,
				timestamp: Date.now(),
			};
			runtime.latestVerification = persisted;
			pi.appendEntry("synth.verifier.decision", persisted);
			const pointer = transcript(ctx, assistantEntryId);
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "bridge",
				...(pointer ? { transcript: pointer } : {}),
				data: {
					action: "verifier.decision",
					assistantEntryId,
					cycle,
					mode,
					decision: result.decision,
					justification: result.justification,
					trajectorySummary: result.trajectorySummary,
					details: result.details,
				},
			});
			const snapshot = budget.snapshot();
			if (
				result.decision !== "SUCCESS" &&
				cycle < config.verifier.maxCycles &&
				!snapshot.fullyExhausted &&
				!ctx.hasPendingMessages()
			) {
				runtime.nudgePending = false;
				pi.sendMessage(
					{
						customType: "synth.verifier.guidance",
						content: formatVerifierGuidance(result, snapshot),
						display: true,
						details: { decision: result.decision, cycle, assistantEntryId },
					},
					{ triggerTurn: true, deliverAs: "followUp" },
				);
				return true;
			}
		} catch (error) {
			warn(
				runtime,
				ctx,
				"verifier",
				`strategic verifier unavailable; accepting the agent conclusion without guidance: ${errorMessage(error)}`,
			);
			const pointer = transcript(ctx, assistantEntryId);
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "bridge",
				...(pointer ? { transcript: pointer } : {}),
				errorClass: "VerifierUnavailable",
				data: { action: "verifier.failed", assistantEntryId, cycle, mode },
			});
		} finally {
			runtime.verificationInFlight = false;
		}
		return false;
	}

	async function loadMemoryForTask(runtime: SessionRuntime, ctx: ExtensionContext, taskQuery: string): Promise<void> {
		const config = runtime.configLoad.config;
		if (!config?.memory.enabled || config.killSwitch.all) {
			return;
		}
		if (!runtime.taskQuery) {
			runtime.taskQuery = taskQuery.trim();
		}
		if (!runtime.taskQuery || runtime.memoryLookup) {
			await runtime.memoryLookup;
			return;
		}
		const client = bridgeFor(runtime, ctx);
		if (!client) {
			return;
		}
		runtime.memoryLookup = (async () => {
			let strategyItems: ReturnType<typeof parseMemoryItems> = [];
			let framingItems: ReturnType<typeof parseMemoryItems> = [];
			const retrievalFailure = (category: "lessons" | "framing", error: unknown): void => {
				warn(
					runtime,
					ctx,
					`memory-${category}`,
					`ReasoningBank ${category} retrieval unavailable; continuing without that section: ${errorMessage(error)}`,
				);
				const pointer = transcript(ctx);
				appendTelemetry(runtime, ctx, {
					sessionId: ctx.sessionManager.getSessionId(),
					kind: "bridge",
					...(pointer ? { transcript: pointer } : {}),
					errorClass: "MemoryRetrievalUnavailable",
					data: { action: `memory.${category}_retrieval_failed` },
				});
			};
			try {
				const response = await client.request("memory.query", {
					query: runtime.taskQuery,
					taskDescription: REASONING_BANK_TASK_DESCRIPTION,
					k: config.memory.k,
					minScore: config.memory.minScore,
					statuses: ["success", "fail"],
					itemTypes: ["strategy"],
				});
				strategyItems = parseMemoryItems(response, true).filter((item) => item.itemType === "strategy");
			} catch (error) {
				retrievalFailure("lessons", error);
			}
			try {
				const framingResponse = await client.request("memory.query", {
					query: runtime.taskQuery,
					taskDescription:
						"Given prior contrastive problem-framing memories, select the most relevant heuristic for framing the current software engineering task before acting.",
					k: 1,
					minScore: config.memory.minScore,
					statuses: ["fail"],
					itemTypes: ["meta_heuristic"],
				});
				framingItems = parseMemoryItems(framingResponse, true)
					.filter((item) => item.itemType === "meta_heuristic" && item.status === "fail")
					.slice(0, 1);
			} catch (error) {
				retrievalFailure("framing", error);
			}
			try {
				const section = renderLessons(strategyItems);
				const framingSection = renderProblemFraming(framingItems);
				if (!section && !framingSection) return;
				runtime.memorySection = section;
				runtime.memoryItemIds = strategyItems.map((item) => item.id);
				runtime.framingSection = framingSection;
				runtime.framingItemIds = framingItems.map((item) => item.id);
				if (!runtime.memoryPersisted) {
					pi.appendEntry("synth.memory.retrieval", {
						taskQuery: runtime.taskQuery,
						...(section ? { section } : {}),
						itemIds: runtime.memoryItemIds,
						...(framingSection ? { framingSection } : {}),
						framingItemIds: runtime.framingItemIds,
						timestamp: Date.now(),
					});
					runtime.memoryPersisted = true;
				}
				const pointer = transcript(ctx);
				appendTelemetry(runtime, ctx, {
					sessionId: ctx.sessionManager.getSessionId(),
					kind: "bridge",
					...(pointer ? { transcript: pointer } : {}),
					data: {
						action: "memory.retrieved",
						itemIds: runtime.memoryItemIds,
						framingItemIds: runtime.framingItemIds,
					},
				});
			} catch (error) {
				retrievalFailure("lessons", error);
			}
		})();
		await runtime.memoryLookup;
	}

	function enqueueDistillation(runtime: SessionRuntime, ctx: ExtensionContext, reason: string): void {
		const config = runtime.configLoad.config;
		const sessionFile = ctx.sessionManager.getSessionFile();
		if (
			runtime.distillEnqueued ||
			process.env.SYNTH_BATCH_READ_ONLY_MEMORY === "1" ||
			(managedBatchSession(ctx) && !runtime.outcomePersisted) ||
			reason === "reload" ||
			!config?.memory.enabled ||
			config.killSwitch.all ||
			!runtime.taskQuery ||
			!sessionFile ||
			!hasMeaningfulTrajectory(ctx)
		) {
			return;
		}
		runtime.distillEnqueued = true;
		const jobId = randomUUID();
		const args = ["run", "--project", ctx.cwd, "--no-sync", "synth", "distill", sessionFile];
		const fixture = process.env.SYNTH_MEMORY_FIXTURE;
		if (fixture) {
			args.push("--fixture", fixture);
		}
		try {
			const child = spawn("uv", args, {
				cwd: ctx.cwd,
				detached: true,
				env: { ...process.env, UV_NO_PROGRESS: "1" },
				stdio: "ignore",
			});
			child.once("error", (error) => {
				if (!runtime.warnings.has("memory-distill")) {
					runtime.warnings.add("memory-distill");
					console.error(`synth: ReasoningBank distillation did not start: ${error.message}`);
				}
			});
			child.unref();
			const pointer = transcript(ctx);
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "bridge",
				...(pointer ? { transcript: pointer } : {}),
				data: {
					action: "memory.distill_enqueued",
					jobId,
					pid: child.pid ?? null,
					reason,
					sessionFile,
				},
			});
		} catch (error) {
			warn(runtime, ctx, "memory-distill", `ReasoningBank distillation did not start: ${errorMessage(error)}`);
		}
	}

	async function statusText(runtime: SessionRuntime, ctx: ExtensionCommandContext): Promise<string> {
		const configLoad = runtime.configLoad;
		if (configLoad.state === "absent") {
			return `Synth status\nconfig=absent (${configLoad.path})\nfeatures=off\nbridge=disabled`;
		}
		if (configLoad.state === "invalid" || !configLoad.config) {
			return `Synth status\nconfig=invalid (${configLoad.path})\nerror=${configLoad.error ?? "unknown"}\nfeatures=off\nbridge=disabled`;
		}
		const config = configLoad.config;
		const client = bridgeFor(runtime, ctx);
		let bridgeLine = "bridge=disabled";
		if (client) {
			try {
				const ping = await client.ping();
				const status = client.getStatus();
				bridgeLine = `bridge=up pid=${status.pid ?? "unknown"} protocol=${ping.protocolVersion} memory=${ping.memoryAvailable ? "ready" : "pending"}`;
			} catch (error) {
				bridgeLine = `bridge=${client.getStatus().state} error=${errorMessage(error)}`;
			}
		}
		return [
			"Synth status",
			`config=loaded (${configLoad.path})`,
			`configEcho=${JSON.stringify(config)}`,
			`endpoint=${config.endpoint?.baseUrl ?? "unset"} model=${config.endpoint?.model ?? "unset"}`,
			`telemetry=${runtime.telemetry?.path ?? "off"}`,
			bridgeLine,
			`memory=${config.memory.enabled ? "configured" : "off"}`,
			`skillCompiler=${config.skillCompiler.enabled ? "configured" : "off"} admitted=${runtime.compileRequests.size}/${config.skillCompiler.maxPerSession} active=${runtime.compileInFlight ? "yes" : "no"}`,
			`budgets=${formatBudgets(config, runtime.budget)}`,
		].join("\n");
	}

	async function memoryText(runtime: SessionRuntime, args: string, ctx: ExtensionCommandContext): Promise<string> {
		const config = runtime.configLoad.config;
		if (runtime.configLoad.state !== "loaded" || !config || !config.memory.enabled) {
			return "Synth memory is off.";
		}
		const client = bridgeFor(runtime, ctx);
		if (!client) {
			return "Synth memory bridge is unavailable.";
		}
		const [operation = "", ...rest] = args.trim().split(/\s+/).filter(Boolean);
		try {
			if (operation === "list" && rest.length === 0) {
				return renderMemoryList(parseMemoryItems(await client.request("memory.list", { limit: 50 }), false));
			}
			if (operation === "search" && rest.length > 0) {
				const query = rest.join(" ");
				const result = await client.request("memory.query", {
					query,
					taskDescription: REASONING_BANK_TASK_DESCRIPTION,
					k: config.memory.k,
					minScore: config.memory.minScore,
					statuses: ["success", "fail"],
					itemTypes: ["strategy"],
				});
				return renderMemorySearch(query, parseMemoryItems(result, true));
			}
			return "Usage: /synth memory [list|search <query>]";
		} catch (error) {
			return `Synth memory unavailable: ${errorMessage(error)}`;
		}
	}

	pi.registerTool({
		name: "compile_skill",
		label: "Compile Skill",
		description:
			"Schedule a recurring multi-step workflow for local generation, deterministic quarantine gating, and promotion as a Python-backed project skill. Use only after the workflow has repeated and stabilized.",
		promptGuidelines: [
			"Call compile_skill only for a reusable workflow with at least two nontrivial steps.",
			"Compilation runs after the turn and may be rejected; never claim promotion from the scheduling receipt.",
		],
		parameters: CompileSkillParameters,
		executionMode: "sequential",
		execute: async (_toolCallId, params, _signal, _onUpdate, ctx) => {
			const result = queueCompile(runtimeFor(ctx), ctx, {
				workflow: params.workflow,
				...(params.nameHint === undefined ? {} : { nameHint: params.nameHint }),
				reason: params.reason,
				source: "tool",
			});
			return {
				content: [{ type: "text", text: result }],
				details: { scheduled: result.startsWith("Synth skill compilation scheduled") },
			};
		},
	});

	pi.registerCommand("synth", {
		description: "Show Synth state or manage memory, budgets, and compiled skills",
		getArgumentCompletions: (prefix) => {
			const commands = [
				"status",
				"memory list",
				"memory search ",
				"budget",
				"compile ",
				"reload-skills",
				"demote ",
			];
			const matches = commands.filter((command) => command.startsWith(prefix));
			return matches.length > 0 ? matches.map((command) => ({ value: command, label: command })) : null;
		},
		handler: async (args, ctx) => {
			const runtime = runtimeFor(ctx);
			const [subcommand = "status", ...rest] = args.trim().split(/\s+/).filter(Boolean);
			if (subcommand === "outcome") {
				const [token, encodedReceipt, ...extra] = rest;
				if (
					!BATCH_OUTCOME_TOKEN ||
					!token ||
					!encodedReceipt ||
					extra.length > 0 ||
					!secureTokenEquals(token, BATCH_OUTCOME_TOKEN)
				) {
					commandOutput(pi, "Synth terminal outcome rejected.");
					return;
				}
				try {
					const decoded = Buffer.from(encodedReceipt, "base64url");
					if (decoded.toString("base64url") !== encodedReceipt) {
						throw new Error("receipt encoding is not canonical base64url");
					}
					const receipt = parseTerminalOutcomeReceipt(JSON.parse(decoded.toString("utf8")));
					const runId = process.env.SYNTH_BATCH_RUN_ID;
					const trajectoryIndex = Number(process.env.SYNTH_BATCH_TRAJECTORY_INDEX);
					if (
						receipt.run_id !== runId ||
						receipt.trajectory_index !== trajectoryIndex ||
						(receipt.session_id !== null && receipt.session_id !== ctx.sessionManager.getSessionId())
					) {
						throw new Error("receipt identity does not match the active batch session");
					}
					runtime.batchOutcome = receipt.outcome;
					commandOutput(pi, "Synth terminal outcome accepted.");
				} catch (error) {
					warn(runtime, ctx, "terminal-outcome-command", `terminal outcome command rejected: ${errorMessage(error)}`);
					commandOutput(pi, "Synth terminal outcome rejected.");
				}
				return;
			}
			if (subcommand === "status") {
				commandOutput(pi, await statusText(runtime, ctx));
				return;
			}
			if (subcommand === "memory") {
				commandOutput(pi, await memoryText(runtime, rest.join(" "), ctx));
				return;
			}
			if (subcommand === "budget") {
				const config = runtime.configLoad.config;
				commandOutput(
					pi,
					config
						? runtime.budget
							? `Synth budget state${formatBudgetBlock(runtime.budget.snapshot())}`
							: `Synth budget configuration\n${formatBudgets(config)}`
						: "Synth budgets are off.",
				);
				return;
			}
			if (subcommand === "compile") {
				const workflow = rest.join(" ").trim();
				if (workflow && workflow.length < 20) {
					commandOutput(pi, "A supplied workflow must contain at least 20 characters.");
					return;
				}
				commandOutput(
					pi,
					queueCompile(runtime, ctx, {
						...(workflow ? { workflow } : {}),
						reason: workflow
							? "User requested compilation of the described recurring workflow"
							: "User requested telemetry mining for a recurring workflow",
						source: "command",
					}),
				);
				return;
			}
			if (subcommand === "reload-skills") {
				if (
					runtime.compileInFlight ||
					[...runtime.compileRequests.values()].some(
						(request) => request.status === "scheduled" || request.status === "started",
					)
				) {
					commandOutput(pi, "Synth cannot reload while a compiler job is pending; wait for its terminal receipt.");
					return;
				}
				commandOutput(pi, "Reloading Synth skills and kernel provisioning...");
				await ctx.reload();
				return;
			}
			if (subcommand === "demote") {
				const name = rest.join(" ").trim();
				if (!name) {
					commandOutput(pi, "Usage: /synth demote <skill-name>");
					return;
				}
				const result = await pi.exec(
					"uv",
					[
						"run",
						"--project",
						ctx.cwd,
						"--no-sync",
						"synth",
						"skill",
						"demote",
						name,
						"--reason",
						"Requested through /synth demote",
					],
					{ cwd: ctx.cwd, timeout: 30_000, env: { UV_NO_PROGRESS: "1" } },
				);
				if (result.code !== 0) {
					commandOutput(pi, `Synth demotion failed: ${(result.stderr || result.stdout).trim()}`);
					return;
				}
				commandOutput(pi, result.stdout.trim() || `Synth demoted ${name}.`);
				await ctx.reload();
				return;
			}
			commandOutput(
				pi,
				"Usage: /synth [status|memory list|memory search <query>|budget|compile [workflow]|reload-skills|demote <name>]",
			);
		},
	});

	pi.on("session_start", (event, ctx) => {
		const runtime = createRuntime(ctx.cwd);
		runtimes.set(ctx.sessionManager, runtime);
		const controlPlaneWarning = managedControlPlaneWarning(ctx.cwd);
		if (controlPlaneWarning) {
			warn(runtime, ctx, "control-plane-permissions", controlPlaneWarning);
		}
		const config = runtime.configLoad.config;
		if (runtime.configLoad.state === "invalid") {
			warn(runtime, ctx, "config", `invalid config; running vanilla: ${runtime.configLoad.error ?? "unknown error"}`);
			return;
		}
		if (runtime.configLoad.state !== "loaded" || !config || config.killSwitch.all) {
			return;
		}
		if (config.memory.enabled) restorePersistedMemory(runtime, ctx);
		restorePersistedVerifications(runtime, ctx);
		restorePersistedCompiles(runtime, ctx);
		runtime.taskQuery ??= firstTaskFromBranch(ctx);
		runtime.initialBudgetInjected = ctx.sessionManager
			.getBranch()
			.some((entry) => entry.type === "message" && messageContains(entry.message, "<budget>"));
		let telemetryFailed = false;
		try {
			const telemetryDir = resolveDataPlanePath(ctx.cwd, config.telemetry.dir);
			runtime.telemetry = new TelemetryWriter(telemetryDir, ctx.sessionManager.getSessionId());
			runtime.outcomePersisted = runtime.telemetry.hasTerminalOutcome();
		} catch (error) {
			telemetryFailed = true;
			runtime.telemetry = undefined;
			warn(runtime, ctx, "telemetry", `telemetry unavailable; running vanilla: ${errorMessage(error)}`);
		}
		try {
			const batchInitialUsed = batchBudgetUsedFromEnvironment();
			runtime.budget = runtime.telemetry
				? BudgetTracker.fromTelemetry(config.budgets, runtime.telemetry.path, batchInitialUsed)
				: telemetryFailed
					? BudgetTracker.exhausted(config.budgets)
					: new BudgetTracker(config.budgets, batchInitialUsed);
		} catch (error) {
			runtime.budget = BudgetTracker.exhausted(config.budgets);
			warn(runtime, ctx, "budget", `budget recovery failed closed: ${errorMessage(error)}`);
		}
		const pointer = transcript(ctx);
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "session.start",
			...(pointer ? { transcript: pointer } : {}),
			data: { reason: event.reason, configPath: runtime.configLoad.path },
		});
		persistBudget(runtime, ctx, "session_start", { restored: event.reason === "resume" });
		resumePersistedCompiler(runtime, ctx);
	});

	pi.on("session_before_compact", async (event, ctx) => {
		const runtime = runtimeFor(ctx);
		if (verifierMode(runtime) !== "batch" || !runtime.taskQuery || event.signal.aborted) {
			return undefined;
		}
		const { preparation } = event;
		const messages = [...preparation.messagesToSummarize, ...preparation.turnPrefixMessages];
		const conversation = serializeConversation(convertToLlm(messages));
		const activePlan = extractLastPlan(`${preparation.previousSummary ?? ""}\n${conversation}`);
		try {
			const generated = await verifierFor(runtime).summarize(
				buildCompactionPrompt(
					runtime.taskQuery,
					conversation,
					preparation.previousSummary,
					runtime.latestVerification?.trajectorySummary,
					activePlan,
				),
			);
			if (event.signal.aborted || !generated.trim()) return undefined;
			const summary = appendActivePlan(generated, activePlan);
			const pointer = transcript(ctx);
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "bridge",
				...(pointer ? { transcript: pointer } : {}),
				data: { action: "verifier.compaction", preservedPlan: activePlan !== undefined },
			});
			return {
				compaction: {
					summary,
					firstKeptEntryId: preparation.firstKeptEntryId,
					tokensBefore: preparation.tokensBefore,
				},
			};
		} catch (error) {
			warn(
				runtime,
				ctx,
				"verifier-compaction",
				`batch compaction summary unavailable; using native compaction: ${errorMessage(error)}`,
			);
			return undefined;
		}
	});

	pi.on("session_before_refine", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		let marker: CompileMarker | undefined;
		try {
			marker = parseCompileMarker(event.preparation.instructions);
		} catch (error) {
			warn(runtime, ctx, "skill-compile-marker", errorMessage(error));
			return {
				proposal: {
					summary: "Rejected invalid Synth skill compilation marker",
					rationale: "The marker did not satisfy the strict local compiler protocol.",
					expectedOutcome: "No compiler work is started.",
					edits: [],
				},
			};
		}
		if (!marker) return undefined;
		const admission = admitCompile(runtime, ctx, marker);
		if (!admission.request) {
			warn(runtime, ctx, "skill-compile-admission", admission.error ?? "skill compilation was rejected");
			return {
				proposal: {
					summary: "Rejected Synth skill compilation request",
					rationale: admission.error ?? "The request could not be admitted safely.",
					expectedOutcome: "No compiler work is started.",
					edits: [],
				},
			};
		}
		runtime.activeCompileRequest = admission.request;
		return {
			proposal: {
				summary: `Scheduled Synth skill compilation ${admission.request.requestId}`,
				rationale:
					"A zero-edit refinement receipt serializes the compiler with native HarnessState mutation.",
				expectedOutcome:
					"The Synth CLI generates, gates, audits, and promotes at most one quarantined package.",
				edits: [],
			},
		};
	});

	pi.on("refine_complete", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		const request = runtime.activeCompileRequest;
		if (
			!request ||
			runtime.compileInFlight ||
			event.summary !== `Scheduled Synth skill compilation ${request.requestId}`
		) {
			return;
		}
		runtime.activeCompileRequest = undefined;
		launchCompiler(runtime, ctx, request);
	});

	pi.on("session_shutdown", async (event, ctx) => {
		const runtime = runtimes.get(ctx.sessionManager);
		if (!runtime) {
			return;
		}
		runtime.shuttingDown = true;
		persistBudget(runtime, ctx, "session_shutdown");
		const pointer = transcript(ctx);
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "session.shutdown",
			...(pointer ? { transcript: pointer } : {}),
			data: { reason: event.reason },
		});
		await runtime.bridge?.close();
		if (event.reason !== "reload") persistTerminalOutcome(runtime, ctx);
		enqueueDistillation(runtime, ctx, event.reason);
		runtimes.delete(ctx.sessionManager);
	});

	pi.on("turn_start", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		runtime.allowedToolCallsThisTurn = 0;
		const startedAt = performance.now();
		runtime.turnStarts.set(event.turnIndex, startedAt);
		runtime.providers.set(event.turnIndex, {
			turnIndex: event.turnIndex,
			requestId: randomUUID(),
			startedAt,
			payloadDigest: digestValue(null),
			provider: ctx.model?.provider ?? null,
			model: ctx.model?.id ?? null,
		});
	});

	pi.on("before_agent_start", async (event, ctx) => {
		const runtime = runtimeFor(ctx);
		runtime.taskQuery ??= cleanTaskPrompt(event.prompt);
		const systemPrompt = runtime.budget
			? injectBatsSystemPrompt(event.systemPrompt, runtime.budget.snapshot())
			: event.systemPrompt;
		await loadMemoryForTask(runtime, ctx, event.prompt);
		const sections: string[] = [];
		if (runtime.memorySection && !systemPrompt.includes(LESSONS_HEADING)) {
			sections.push(runtime.memorySection);
		}
		if (runtime.framingSection && !systemPrompt.includes(PROBLEM_FRAMING_HEADING)) {
			sections.push(runtime.framingSection);
		}
		const combined =
			sections.length > 0 ? `${systemPrompt.trimEnd()}\n\n${sections.join("\n\n")}` : systemPrompt;
		return combined === event.systemPrompt ? undefined : { systemPrompt: combined };
	});

	pi.on("context", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		let messages = event.messages;
		if (runtime.budget) {
			const snapshot = runtime.budget.snapshot();
			let firstUserSeen = false;
			messages = elideOldToolResponses(
				messages.map((message) => {
					if (message.role !== "user" || firstUserSeen) {
						return message;
					}
					firstUserSeen = true;
					return appendBudgetToUser(message, snapshot);
				}),
				runtime.budget.config.elideToolResponsesAfterTurns,
			).map((message) => appendBudgetToToolResult(message, snapshot));
			const latest = messages.at(-1);
			if (snapshot.fullyExhausted && (!latest || !messageContains(latest, BATS_WRAP_UP_INSTRUCTION))) {
				messages.push({
					role: "user",
					content: `${BATS_WRAP_UP_INSTRUCTION}${formatBudgetBlock(snapshot)}`,
					timestamp: Date.now(),
				});
			}
		}
		const provider = latestProvider(runtime);
		if (provider) {
			provider.startedAt = performance.now();
			provider.payloadDigest = digestValue(messages);
			provider.provider = ctx.model?.provider ?? provider.provider;
			provider.model = ctx.model?.id ?? provider.model;
		}
		return messages === event.messages ? undefined : { messages };
	});

	pi.on("message_end", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		const budget = runtime.budget;
		if (!budget) {
			return undefined;
		}
		if (event.message.role === "user" && !runtime.initialBudgetInjected) {
			runtime.initialBudgetInjected = true;
			const message = appendBudgetToUser(event.message, budget.snapshot());
			return message === event.message ? undefined : { message };
		}
		if (event.message.role === "assistant") {
			const update = budget.addUsage(event.message.usage);
			persistBudget(runtime, ctx, "provider_usage", {
				regimeChanged: update.regimeChanged,
				previousRegime: update.before.regime,
			});
			return undefined;
		}
		if (event.message.role === "toolResult") {
			const message = appendBudgetToToolResult(event.message, budget.snapshot());
			return message === event.message ? undefined : { message };
		}
		return undefined;
	});

	pi.on("before_provider_request", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		const provider = latestProvider(runtime);
		if (provider) {
			provider.startedAt = performance.now();
			provider.payloadDigest = digestValue(event.payload);
			provider.provider = ctx.model?.provider ?? provider.provider;
			provider.model = ctx.model?.id ?? provider.model;
		}
	});

	pi.on("after_provider_response", (event, ctx) => {
		const provider = firstProviderWithoutStatus(runtimeFor(ctx));
		if (provider) {
			provider.status = event.status;
		}
	});

	pi.on("turn_end", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		const completedAt = performance.now();
		const startedAt = runtime.turnStarts.get(event.turnIndex);
		runtime.turnStarts.delete(event.turnIndex);
		const provider = runtime.providers.get(event.turnIndex);
		runtime.providers.delete(event.turnIndex);
		const usage = assistantUsage(event.message);
		const entryId = messageEntryId(ctx, event.message);
		const pointer = transcript(ctx, entryId);
		if (provider) {
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "provider",
				turnIndex: event.turnIndex,
				durationMs: Math.max(0, completedAt - provider.startedAt),
				...(usage ? { usage: copyUsage(usage) } : {}),
				provider: {
					requestId: provider.requestId,
					payloadDigest: provider.payloadDigest,
					provider: provider.provider,
					model: provider.model,
					status: provider.status ?? 0,
				},
				...(pointer ? { transcript: pointer } : {}),
				...(provider.status !== undefined && provider.status >= 400
					? { errorClass: "ProviderHttpError" }
					: assistantErrorClass(event.message)
						? { errorClass: assistantErrorClass(event.message) }
						: {}),
			});
		}
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "turn",
			turnIndex: event.turnIndex,
			...(startedAt === undefined ? {} : { durationMs: Math.max(0, completedAt - startedAt) }),
			...(usage ? { usage: copyUsage(usage) } : {}),
			...(pointer ? { transcript: pointer } : {}),
			...(assistantErrorClass(event.message) ? { errorClass: assistantErrorClass(event.message) } : {}),
			data: { toolResults: event.toolResults.length, role: event.message.role },
		});
		if (runtime.budget) {
			if (runtime.allowedToolCallsThisTurn > 0) {
				runtime.noToolCount = 0;
			} else if (assistantStalled(event.message)) {
				runtime.noToolCount++;
			} else {
				runtime.noToolCount = 0;
			}
			const snapshot = runtime.budget.snapshot();
			runtime.nudgePending = shouldSendContinuationNudge(
				runtime.noToolCount,
				runtime.budget.config.noneAnswerThreshold,
				runtime.budget.hasUsefulToolBudget(),
				snapshot.fullyExhausted,
			);
		}
	});

	pi.on("tool_result", (event, ctx) => {
		const budget = runtimeFor(ctx).budget;
		if (
			!budget ||
			contentText(event.content).includes("<budget>") ||
			contentText(event.content).includes(BATS_ELISION_MARKER)
		) {
			return undefined;
		}
		return {
			content: [...event.content, { type: "text", text: toolBudgetText(budget.snapshot()) }],
		};
	});

	pi.on("tool_execution_start", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		runtime.toolStarts.set(event.toolCallId, {
			startedAt: performance.now(),
			argsDigest: digestValue(event.args),
			entryId: toolCallEntryId(ctx, event.toolCallId),
		});
	});

	pi.on("tool_execution_end", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		const started = runtime.toolStarts.get(event.toolCallId);
		runtime.toolStarts.delete(event.toolCallId);
		const pointer = transcript(ctx, started?.entryId ?? toolCallEntryId(ctx, event.toolCallId));
		runtime.lastToolError = event.isError ? pointer : undefined;
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "tool",
			durationMs: started ? Math.max(0, performance.now() - started.startedAt) : 0,
			tool: {
				toolCallId: event.toolCallId,
				name: event.toolName,
				argsDigest: started?.argsDigest ?? digestValue(null),
				isError: event.isError,
			},
			...(pointer ? { transcript: pointer } : {}),
			...(event.isError ? { errorClass: resultErrorClass(event.result) } : {}),
		});
	});

	pi.on("agent_end", async (event, ctx) => {
		const runtime = runtimeFor(ctx);
		const lastMessage = event.messages.at(-1);
		const pointer = transcript(ctx, lastMessage ? messageEntryId(ctx, lastMessage) : ctx.sessionManager.getLeafId());
		runtime.terminalToolError =
			lastMessage?.role === "assistant" && lastMessage.stopReason === "error" ? runtime.lastToolError : undefined;
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "agent.end",
			...(pointer ? { transcript: pointer } : {}),
			...(lastMessage && assistantErrorClass(lastMessage)
				? { errorClass: assistantErrorClass(lastMessage) }
				: {}),
			data: { messages: event.messages.length, abandonedProviders: runtime.providers.size },
		});
		runtime.providers.clear();
		const verifierFollowUpQueued = await verifyConclusion(runtime, ctx, event.messages);
		if (!verifierFollowUpQueued && runtime.nudgePending && runtime.budget && !ctx.hasPendingMessages()) {
			runtime.nudgePending = false;
			pi.sendMessage(
				{
					customType: "synth.budget.continue",
					content: `${BATS_CONTINUATION_NUDGE}${formatBudgetBlock(runtime.budget.snapshot())}`,
					display: true,
					details: { noToolCount: runtime.noToolCount },
				},
				{ triggerTurn: true, deliverAs: "followUp" },
			);
		}
	});

	pi.on("tool_call", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		let controlReason: string | undefined;
		try {
			controlReason = controlPlaneBlockReason(event, ctx.cwd);
		} catch (error) {
			controlReason = `Synth runtime guard could not validate the tool call: ${errorMessage(error)}`;
		}
		if (controlReason) {
			const pointer = transcript(ctx);
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "tool",
				durationMs: 0,
				tool: {
					toolCallId: event.toolCallId,
					name: event.toolName,
					argsDigest: digestValue(event.input),
					isError: true,
				},
				...(pointer ? { transcript: pointer } : {}),
				errorClass: "ControlPlaneWriteBlocked",
				data: { denied: true },
			});
			return { block: true, reason: controlReason };
		}
		if (!runtime.budget) {
			runtime.allowedToolCallsThisTurn++;
			return undefined;
		}
		try {
			const reservation = runtime.budget.reserve(event);
			if (reservation.allowed) {
				runtime.allowedToolCallsThisTurn++;
				if (reservation.charges.length > 0) {
					persistBudget(runtime, ctx, "tool_reserved", {
						toolCallId: event.toolCallId,
						toolName: event.toolName,
						charges: reservation.charges,
						regimeChanged: reservation.regimeChanged,
						previousRegime: reservation.before.regime,
					});
				}
				return undefined;
			}
			persistBudget(runtime, ctx, "tool_denied", {
				toolCallId: event.toolCallId,
				toolName: event.toolName,
				charges: reservation.charges,
			});
			const pointer = transcript(ctx);
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "tool",
				durationMs: 0,
				tool: {
					toolCallId: event.toolCallId,
					name: event.toolName,
					argsDigest: digestValue(event.input),
					isError: true,
				},
				...(pointer ? { transcript: pointer } : {}),
				errorClass: "BudgetExceeded",
				data: { denied: true },
			});
			const reason = reservation.after.fullyExhausted
				? `${reservation.reason ?? "No tool budget left."}\n${BATS_WRAP_UP_INSTRUCTION}`
				: (reservation.reason ?? "No tool budget left.");
			return { block: true, reason };
		} catch (error) {
			const reason = `Synth budget guard failed closed: ${errorMessage(error)}`;
			warn(runtime, ctx, "budget-guard", reason);
			return { block: true, reason };
		}
	});
}
