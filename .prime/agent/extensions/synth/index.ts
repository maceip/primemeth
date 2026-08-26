import { randomUUID } from "node:crypto";
import { spawn } from "node:child_process";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { Usage } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { BridgeClient } from "./bridge.js";
import {
	assistantStalled,
	BATS_CONTINUATION_NUDGE,
	BATS_ELISION_MARKER,
	BATS_WRAP_UP_INSTRUCTION,
	BudgetTracker,
	elideOldToolResponses,
	formatBudgetBlock,
	formatBudgetStatusLine,
	injectBatsSystemPrompt,
	shouldSendContinuationNudge,
} from "./budget.js";
import { loadSynthConfig, resolveDataPlanePath, type SynthConfig, type SynthConfigLoadResult } from "./config.js";
import { controlPlaneBlockReason } from "./guard.js";
import {
	LESSONS_HEADING,
	parseMemoryItems,
	parsePersistedMemoryRetrieval,
	REASONING_BANK_TASK_DESCRIPTION,
	renderLessons,
	renderMemoryList,
	renderMemorySearch,
} from "./memory.js";
import {
	type BudgetSnapshot,
	copyUsage,
	digestValue,
	TelemetryWriter,
	type TelemetryEventInput,
} from "./telemetry.js";

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
	memoryPersisted: boolean;
	distillEnqueued: boolean;
}

function errorMessage(error: unknown): string {
	return error instanceof Error ? error.message : String(error);
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
		memoryPersisted: false,
		distillEnqueued: false,
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

	function appendTelemetry(runtime: SessionRuntime, ctx: ExtensionContext, event: TelemetryEventInput): void {
		if (!runtime.telemetry) {
			return;
		}
		try {
			runtime.telemetry.append(event);
		} catch (error) {
			runtime.telemetry = undefined;
			warn(runtime, ctx, "telemetry", `telemetry disabled after a write failure: ${errorMessage(error)}`);
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
			try {
				const response = await client.request("memory.query", {
					query: runtime.taskQuery,
					taskDescription: REASONING_BANK_TASK_DESCRIPTION,
					k: config.memory.k,
					minScore: config.memory.minScore,
					statuses: ["success", "fail"],
					itemTypes: ["strategy"],
				});
				const items = parseMemoryItems(response, true);
				const section = renderLessons(items);
				if (!section) {
					return;
				}
				runtime.memorySection = section;
				runtime.memoryItemIds = items.map((item) => item.id);
				if (!runtime.memoryPersisted) {
					pi.appendEntry("synth.memory.retrieval", {
						taskQuery: runtime.taskQuery,
						section,
						itemIds: runtime.memoryItemIds,
						timestamp: Date.now(),
					});
					runtime.memoryPersisted = true;
				}
				const pointer = transcript(ctx);
				appendTelemetry(runtime, ctx, {
					sessionId: ctx.sessionManager.getSessionId(),
					kind: "bridge",
					...(pointer ? { transcript: pointer } : {}),
					data: { action: "memory.retrieved", itemIds: runtime.memoryItemIds },
				});
			} catch (error) {
				warn(runtime, ctx, "memory", `ReasoningBank retrieval unavailable; continuing without lessons: ${errorMessage(error)}`);
				const pointer = transcript(ctx);
				appendTelemetry(runtime, ctx, {
					sessionId: ctx.sessionManager.getSessionId(),
					kind: "bridge",
					...(pointer ? { transcript: pointer } : {}),
					errorClass: "MemoryRetrievalUnavailable",
					data: { action: "memory.retrieval_failed" },
				});
			}
		})();
		await runtime.memoryLookup;
	}

	function enqueueDistillation(runtime: SessionRuntime, ctx: ExtensionContext, reason: string): void {
		const config = runtime.configLoad.config;
		const sessionFile = ctx.sessionManager.getSessionFile();
		if (
			runtime.distillEnqueued ||
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

	pi.registerCommand("synth", {
		description: "Show Synth status, memory, or budget state",
		getArgumentCompletions: (prefix) => {
			const commands = ["status", "memory list", "memory search ", "budget"];
			const matches = commands.filter((command) => command.startsWith(prefix));
			return matches.length > 0 ? matches.map((command) => ({ value: command, label: command })) : null;
		},
		handler: async (args, ctx) => {
			const runtime = runtimeFor(ctx);
			const [subcommand = "status", ...rest] = args.trim().split(/\s+/).filter(Boolean);
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
			commandOutput(pi, "Usage: /synth [status|memory list|memory search <query>|budget]");
		},
	});

	pi.on("session_start", (event, ctx) => {
		const runtime = createRuntime(ctx.cwd);
		runtimes.set(ctx.sessionManager, runtime);
		const config = runtime.configLoad.config;
		if (runtime.configLoad.state === "invalid") {
			warn(runtime, ctx, "config", `invalid config; running vanilla: ${runtime.configLoad.error ?? "unknown error"}`);
			return;
		}
		if (runtime.configLoad.state !== "loaded" || !config || config.killSwitch.all) {
			return;
		}
		restorePersistedMemory(runtime, ctx);
		runtime.initialBudgetInjected = ctx.sessionManager
			.getBranch()
			.some((entry) => entry.type === "message" && messageContains(entry.message, "<budget>"));
		let telemetryFailed = false;
		try {
			const telemetryDir = resolveDataPlanePath(ctx.cwd, config.telemetry.dir);
			runtime.telemetry = new TelemetryWriter(telemetryDir, ctx.sessionManager.getSessionId());
		} catch (error) {
			telemetryFailed = true;
			runtime.telemetry = undefined;
			warn(runtime, ctx, "telemetry", `telemetry unavailable; running vanilla: ${errorMessage(error)}`);
		}
		try {
			runtime.budget = runtime.telemetry
				? BudgetTracker.fromTelemetry(config.budgets, runtime.telemetry.path)
				: telemetryFailed
					? BudgetTracker.exhausted(config.budgets)
					: new BudgetTracker(config.budgets);
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
	});

	pi.on("session_shutdown", async (event, ctx) => {
		const runtime = runtimes.get(ctx.sessionManager);
		if (!runtime) {
			return;
		}
		persistBudget(runtime, ctx, "session_shutdown");
		const pointer = transcript(ctx);
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "session.shutdown",
			...(pointer ? { transcript: pointer } : {}),
			data: { reason: event.reason },
		});
		await runtime.bridge?.close();
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
		const systemPrompt = runtime.budget
			? injectBatsSystemPrompt(event.systemPrompt, runtime.budget.snapshot())
			: event.systemPrompt;
		await loadMemoryForTask(runtime, ctx, event.prompt);
		const memorySection = runtime.memorySection;
		const combined =
			memorySection && !systemPrompt.includes(LESSONS_HEADING)
				? `${systemPrompt.trimEnd()}\n\n${memorySection}`
				: systemPrompt;
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

	pi.on("agent_end", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		const lastMessage = event.messages.at(-1);
		const pointer = transcript(ctx, lastMessage ? messageEntryId(ctx, lastMessage) : ctx.sessionManager.getLeafId());
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
		if (runtime.nudgePending && runtime.budget && !ctx.hasPendingMessages()) {
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
