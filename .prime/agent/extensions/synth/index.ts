import { randomUUID } from "node:crypto";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { Usage } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { BridgeClient, BridgeRequestError } from "./bridge.js";
import { loadSynthConfig, resolveDataPlanePath, type SynthConfig, type SynthConfigLoadResult } from "./config.js";
import { controlPlaneBlockReason } from "./guard.js";
import { copyUsage, digestValue, TelemetryWriter, type TelemetryEventInput } from "./telemetry.js";

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
	warnings: Set<string>;
	turnStarts: Map<number, number>;
	toolStarts: Map<string, ToolStart>;
	providers: Map<number, ProviderExchange>;
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

function formatBudgets(config: SynthConfig): string {
	const { bash, edit, ipython, subagents, tokens, costUsd } = config.budgets;
	return `bash=${bash} edit=${edit} ipython=${ipython} subagents=${subagents} tokens=${tokens} costUsd=${costUsd}`;
}

function createRuntime(cwd: string): SessionRuntime {
	return {
		configLoad: loadSynthConfig(cwd),
		warnings: new Set(),
		turnStarts: new Map(),
		toolStarts: new Map(),
		providers: new Map(),
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
			`budgets=${formatBudgets(config)}`,
		].join("\n");
	}

	async function memoryText(runtime: SessionRuntime, args: string, ctx: ExtensionCommandContext): Promise<string> {
		const config = runtime.configLoad.config;
		if (runtime.configLoad.state !== "loaded" || !config || !config.memory.enabled) {
			return "Synth memory is off.";
		}
		const query = args.trim();
		if (!query) {
			return "Usage: /synth memory <query>";
		}
		const client = bridgeFor(runtime, ctx);
		if (!client) {
			return "Synth memory bridge is unavailable.";
		}
		try {
			const result = await client.request("memory.query", {
				query,
				taskDescription: "Resolve a software engineering task in a repository",
				k: config.memory.k,
				minScore: config.memory.minScore,
				statuses: ["success", "fail"],
				itemTypes: ["strategy", "meta_heuristic"],
			});
			return `Synth memory\n${JSON.stringify(result, null, 2)}`;
		} catch (error) {
			if (error instanceof BridgeRequestError && error.code === "feature_disabled") {
				return `Synth memory pending Phase 4: ${error.message}`;
			}
			return `Synth memory unavailable: ${errorMessage(error)}`;
		}
	}

	pi.registerCommand("synth", {
		description: "Show Synth status, memory, or budget state",
		getArgumentCompletions: (prefix) => {
			const commands = ["status", "memory", "budget"];
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
					config ? `Synth budget configuration\n${formatBudgets(config)}` : "Synth budgets are off.",
				);
				return;
			}
			commandOutput(pi, "Usage: /synth [status|memory <query>|budget]");
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
		try {
			const telemetryDir = resolveDataPlanePath(ctx.cwd, config.telemetry.dir);
			runtime.telemetry = new TelemetryWriter(telemetryDir, ctx.sessionManager.getSessionId());
			const pointer = transcript(ctx);
			appendTelemetry(runtime, ctx, {
				sessionId: ctx.sessionManager.getSessionId(),
				kind: "session.start",
				...(pointer ? { transcript: pointer } : {}),
				data: { reason: event.reason, configPath: runtime.configLoad.path },
			});
		} catch (error) {
			runtime.telemetry = undefined;
			warn(runtime, ctx, "telemetry", `telemetry unavailable; running vanilla: ${errorMessage(error)}`);
		}
	});

	pi.on("session_shutdown", async (event, ctx) => {
		const runtime = runtimes.get(ctx.sessionManager);
		if (!runtime) {
			return;
		}
		const pointer = transcript(ctx);
		appendTelemetry(runtime, ctx, {
			sessionId: ctx.sessionManager.getSessionId(),
			kind: "session.shutdown",
			...(pointer ? { transcript: pointer } : {}),
			data: { reason: event.reason },
		});
		await runtime.bridge?.close();
		runtimes.delete(ctx.sessionManager);
	});

	pi.on("turn_start", (event, ctx) => {
		const runtime = runtimeFor(ctx);
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

	pi.on("context", (event, ctx) => {
		const provider = latestProvider(runtimeFor(ctx));
		if (provider) {
			provider.startedAt = performance.now();
			provider.payloadDigest = digestValue(event.messages);
			provider.provider = ctx.model?.provider ?? provider.provider;
			provider.model = ctx.model?.id ?? provider.model;
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
	});

	pi.on("tool_call", (event, ctx) => {
		const runtime = runtimeFor(ctx);
		let reason: string | undefined;
		try {
			reason = controlPlaneBlockReason(event, ctx.cwd);
		} catch (error) {
			reason = `Synth runtime guard could not validate the tool call: ${errorMessage(error)}`;
		}
		if (!reason) {
			return undefined;
		}
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
		return { block: true, reason };
	});
}
