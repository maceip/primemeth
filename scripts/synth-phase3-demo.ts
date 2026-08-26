import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { AgentMessage, AgentTool } from "@earendil-works/pi-agent-core";
import {
	type AssistantMessage,
	type Context,
	fauxAssistantMessage,
	fauxToolCall,
	type FauxResponseFactory,
	type Message,
	type Usage,
} from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { Value } from "typebox/value";
import synthExtension from "../.prime/agent/extensions/synth/index.js";
import {
	BATS_CONTINUATION_NUDGE,
	BATS_ELISION_MARKER,
	BATS_SYSTEM_PROMPT_SECTION,
	BATS_WRAP_UP_INSTRUCTION,
	BudgetTracker,
	elideOldToolResponses,
	regimeForFractions,
	shouldSendContinuationNudge,
} from "../.prime/agent/extensions/synth/budget.js";
import type { SynthBudgetConfig } from "../.prime/agent/extensions/synth/config.js";
import {
	TelemetryEventSchema,
	type TelemetryEvent,
} from "../.prime/agent/extensions/synth/telemetry.js";
import { emitSessionShutdownEvent } from "../packages/coding-agent/src/core/extensions/runner.js";
import type { HostRequestHandlers } from "../packages/coding-agent/src/core/kernel/index.js";
import { createHarness } from "../packages/coding-agent/test/suite/harness.js";

const telemetryOutput = process.env.SYNTH_PHASE3_TELEMETRY_OUT;
if (!telemetryOutput) {
	throw new Error("SYNTH_PHASE3_TELEMETRY_OUT is required");
}

const fixtureBudgets: SynthBudgetConfig = {
	bash: 2,
	edit: 1,
	ipython: 1,
	subagents: 1,
	tokens: 0,
	costUsd: 0,
	elideToolResponsesAfterTurns: 1,
	noneAnswerThreshold: 3,
};

function messageText(message: AgentMessage): string {
	if (message.role === "user") {
		return typeof message.content === "string"
			? message.content
			: message.content.flatMap((item) => (item.type === "text" ? [item.text] : [])).join("\n");
	}
	if (message.role === "assistant") {
		return message.content.flatMap((item) => (item.type === "text" ? [item.text] : [])).join("\n");
	}
	if (message.role === "toolResult") {
		return message.content.flatMap((item) => (item.type === "text" ? [item.text] : [])).join("\n");
	}
	return "content" in message && typeof message.content === "string" ? message.content : "";
}

function contextText(context: Context): string {
	return context.messages.map(messageText).join("\n");
}

function capturedResponse(contexts: Context[], message: AssistantMessage): FauxResponseFactory {
	return (context) => {
		contexts.push({ ...context, messages: structuredClone(context.messages) });
		return message;
	};
}

function customCommandMessages(messages: readonly { role: string }[]): string[] {
	return messages.flatMap((message) => {
		if (
			message.role !== "custom" ||
			!("customType" in message) ||
			message.customType !== "synth.command" ||
			!("content" in message) ||
			typeof message.content !== "string"
		) {
			return [];
		}
		return [message.content];
	});
}

function occurrences(text: string, needle: string): number {
	return text.split(needle).length - 1;
}

const elisionFixture: Message[] = [
	fauxAssistantMessage("first turn"),
	{
		role: "toolResult",
		toolCallId: "old-result",
		toolName: "bash",
		content: [{ type: "text", text: "old observation" }],
		isError: false,
		timestamp: 1,
	},
	fauxAssistantMessage("second turn"),
	{
		role: "toolResult",
		toolCallId: "current-result",
		toolName: "bash",
		content: [{ type: "text", text: "current observation" }],
		isError: false,
		timestamp: 2,
	},
];
const usageFixture: Usage = {
	input: 2,
	output: 3,
	cacheRead: 5,
	cacheWrite: 7,
	totalTokens: 17,
	cost: { input: 0.02, output: 0.03, cacheRead: 0.01, cacheWrite: 0.04, total: 0.1 },
};
const usageTracker = new BudgetTracker({ ...fixtureBudgets, tokens: 10, costUsd: 0.05 });
usageTracker.addUsage(usageFixture);
assert.deepEqual(
	{
		regimes: [[], [1, 0.7], [0.699], [0.3], [0.299], [0.1], [0.099], [1, 0]].map(
			regimeForFractions,
		),
		continuation: [
			shouldSendContinuationNudge(1, 3, true, false),
			shouldSendContinuationNudge(3, 3, true, false),
			shouldSendContinuationNudge(1, 3, false, false),
			shouldSendContinuationNudge(1, 3, true, true),
			BATS_CONTINUATION_NUDGE.startsWith("Wait, you still have remaining tool budget"),
		],
		elision: elideOldToolResponses(elisionFixture, 1).map(messageText),
		usage: [
			usageTracker.snapshot().tokens.used,
			usageTracker.snapshot().costUsd.used,
			usageTracker.snapshot().regime,
			usageTracker.snapshot().fullyExhausted,
		],
	},
	{
		regimes: ["HIGH", "HIGH", "MEDIUM", "MEDIUM", "LOW", "LOW", "CRITICAL", "CRITICAL"],
		continuation: [true, false, false, false, true],
		elision: ["first turn", BATS_ELISION_MARKER, "second turn", "current observation"],
		usage: [17, 0.1, "CRITICAL", true],
	},
);

let bashExecutions = 0;
let editExecutions = 0;
let ipythonExecutions = 0;
let subagentToolExecutions = 0;
const bashTool: AgentTool = {
	name: "bash",
	label: "Bash fixture",
	description: "Return deterministic repository evidence",
	parameters: Type.Object({ command: Type.String() }),
	execute: async () => {
		bashExecutions++;
		return {
			content: [
				{
					type: "text",
					text: bashExecutions === 1 ? "old-secret-observation" : "current-evidence",
				},
			],
			details: { execution: bashExecutions },
		};
	},
};
const editTool: AgentTool = {
	name: "edit",
	label: "Edit fixture",
	description: "Exercise the edit budget without changing files",
	parameters: Type.Object({ path: Type.String(), oldText: Type.String(), newText: Type.String() }),
	execute: async () => {
		editExecutions++;
		return { content: [{ type: "text", text: "edit-evidence" }], details: {} };
	},
};
const ipythonTool: AgentTool = {
	name: "ipython",
	label: "IPython fixture",
	description: "Exercise the IPython budget without starting a kernel",
	parameters: Type.Object({ code: Type.String() }),
	execute: async () => {
		ipythonExecutions++;
		return { content: [{ type: "text", text: "ipython-evidence" }], details: {} };
	},
};
const subagentTool: AgentTool = {
	name: "spawn_agent",
	label: "Subagent fixture",
	description: "Exercise the extension-visible subagent budget class",
	parameters: Type.Object({ task: Type.String() }),
	execute: async () => {
		subagentToolExecutions++;
		return { content: [{ type: "text", text: "subagent-evidence" }], details: {} };
	},
};

const harness = await createHarness({
	extensionFactories: [synthExtension],
	persistSession: true,
	tools: [bashTool, editTool, ipythonTool, subagentTool],
});
let shutdownEmitted = false;

try {
	writeFileSync(
		join(harness.tempDir, "synth.json"),
		`${JSON.stringify({ telemetry: { dir: ".synth/telemetry" }, budgets: fixtureBudgets }, null, 2)}\n`,
	);
	await harness.session.bindExtensions({});

	const contexts: Context[] = [];
	harness.setResponses([
		capturedResponse(
			contexts,
			fauxAssistantMessage(fauxToolCall("bash", { command: "inspect first fixture" }), {
				stopReason: "toolUse",
			}),
		),
		capturedResponse(
			contexts,
			fauxAssistantMessage(fauxToolCall("bash", { command: "inspect second fixture" }), {
				stopReason: "toolUse",
			}),
		),
		capturedResponse(
			contexts,
			fauxAssistantMessage(fauxToolCall("bash", { command: "inspect over budget" }), {
				stopReason: "toolUse",
			}),
		),
		capturedResponse(
			contexts,
			fauxAssistantMessage(
				fauxToolCall("edit", { path: ".synth/demo.txt", oldText: "before", newText: "after" }),
				{ stopReason: "toolUse" },
			),
		),
		capturedResponse(
			contexts,
			fauxAssistantMessage(fauxToolCall("ipython", { code: "1 + 1" }), { stopReason: "toolUse" }),
		),
		capturedResponse(
			contexts,
			fauxAssistantMessage(fauxToolCall("spawn_agent", { task: "read-only fixture" }), {
				stopReason: "toolUse",
			}),
		),
		capturedResponse(contexts, fauxAssistantMessage("Phase 3 wrapped up with honest evidence.")),
	]);

	await harness.session.prompt("Exercise BATS budgets and observation hygiene");
	assert.deepEqual(
		[contexts.length, bashExecutions, editExecutions, ipythonExecutions, subagentToolExecutions],
		[7, 2, 1, 1, 1],
	);

	const firstUser = contexts[0].messages.find((message) => message.role === "user");
	const systemMarkers = [
		"### HIGH Budget (≥70% remaining)",
		"### MEDIUM Budget (30%-70%)",
		"### LOW Budget (10%-30%)",
		"### CRITICAL Budget (<10% remaining or 0 in any bounded budget)",
		"[ ] pending, [x] done, [!] failed, [~] partial",
		"Keep all executed steps; never delete them",
	];
	const firstUserText = firstUser ? messageText(firstUser) : "";
	const initialSystemPrompt = contexts[0].systemPrompt ?? "";
	const missingSystemMarkers = systemMarkers.filter((marker) => !initialSystemPrompt.includes(marker));
	const initialEvidence = [
		firstUserText.startsWith("Exercise BATS budgets and observation hygiene"),
		firstUserText.includes("<budget>"),
		firstUserText.includes("Make the best use of the available resources."),
		initialSystemPrompt.includes(BATS_SYSTEM_PROMPT_SECTION),
		missingSystemMarkers.length === 0,
	];
	assert.ok(
		initialEvidence.every(Boolean),
		`initial budget block or BATS system protocol is incomplete; evidence=${initialEvidence.join(",")} missing=${missingSystemMarkers.join(",")}`,
	);
	const contextEvidence = contexts.map(contextText);
	assert.ok(
		contextEvidence[1]?.includes("old-secret-observation") &&
			contextEvidence[1].includes("Bash Budget Used: 1, Bash Budget Remaining: 1") &&
			contextEvidence[1].includes("Regime: MEDIUM") &&
			!contextEvidence[2]?.includes("old-secret-observation") &&
			contextEvidence[2]?.includes(BATS_ELISION_MARKER) &&
			contextEvidence[2].includes("current-evidence") &&
			contextEvidence[2].includes("Bash Budget Used: 2, Bash Budget Remaining: 0") &&
			contextEvidence[2].includes("Regime: CRITICAL") &&
			contextEvidence[3]?.includes("No bash budget left.") &&
			contextEvidence[6]?.includes(BATS_WRAP_UP_INSTRUCTION) &&
			contextEvidence[6].includes("Subagent Budget Used: 1, Subagent Budget Remaining: 0"),
		"persisted context omitted a regime transition, elision, denial, or exhaustion wrap-up",
	);

	await harness.session.prompt("/synth budget");
	const budgetCommand = customCommandMessages(harness.session.messages).at(-1);
	assert.ok(budgetCommand?.includes("Synth budget state") && budgetCommand.includes("Regime: CRITICAL"));

	interface InspectableSession {
		_createKernelHostHandlers(): HostRequestHandlers;
	}
	const hostHandlers = (harness.session as unknown as InspectableSession)._createKernelHostHandlers();
	const runSubagent = hostHandlers["rlm.run"];
	if (!runSubagent) {
		throw new Error("kernel host omitted rlm.run");
	}
	await assert.rejects(
		runSubagent({ prompt: "must be denied before child creation", kwargs: {} }),
		/No subagent budget left\./,
	);

	await emitSessionShutdownEvent(harness.session.extensionRunner, {
		type: "session_shutdown",
		reason: "quit",
	});
	shutdownEmitted = true;

	const nativeMessages = harness.session.messages;
	const nativeUser = nativeMessages.find((message) => message.role === "user");
	const nativeToolResults = nativeMessages.filter((message) => message.role === "toolResult");
	const nativeText = nativeMessages.flatMap((message) => {
		if (message.role === "user" || message.role === "assistant" || message.role === "toolResult") {
			return [messageText(message)];
		}
		return [];
	}).join("\n");
	assert.ok(
		nativeUser?.role === "user" &&
			messageText(nativeUser).includes("<budget>") &&
			nativeToolResults.length === 6 &&
			nativeToolResults.every((result) => occurrences(messageText(result), "<budget>") === 1) &&
			nativeText.includes("old-secret-observation") &&
			nativeText.includes("No bash budget left.") &&
			nativeText.includes(BATS_WRAP_UP_INSTRUCTION),
		"native transcript did not preserve evidence and one budget block per tool result",
	);

	const sessionId = harness.sessionManager.getSessionId();
	const telemetryPath = join(harness.tempDir, ".synth", "telemetry", `${sessionId}.jsonl`);
	const events: TelemetryEvent[] = readFileSync(telemetryPath, "utf8")
		.trim()
		.split("\n")
		.map((line, index) => {
			const value: unknown = JSON.parse(line);
			if (!Value.Check(TelemetryEventSchema, value)) {
				throw new Error(`invalid telemetry event ${index + 1}`);
			}
			return value;
		});
	const budgetEvents = events.filter((event) => event.kind === "budget" && event.budget !== undefined);
	const acceptedBash = budgetEvents.filter(
		(event) => event.data?.reason === "tool_reserved" && event.data.toolName === "bash",
	);
	const latestBudget = budgetEvents.at(-1)?.budget;
	const turnUsage = events
		.filter((event) => event.kind === "turn" && event.usage !== undefined)
		.reduce((total, event) => total + (event.usage?.totalTokens ?? 0), 0);
	const restored = BudgetTracker.fromTelemetry(fixtureBudgets, telemetryPath).snapshot();
	const telemetryEvidence = [
		budgetEvents.length >= 14,
		acceptedBash.map((event) => event.budget?.bash.used).join(",") === "1,2",
		budgetEvents.some(
			(event) =>
				event.data?.reason === "tool_denied" &&
				event.data.toolName === "bash" &&
				event.budget?.bash.used === 2,
		),
		latestBudget?.regime === "CRITICAL",
		latestBudget?.fullyExhausted === true,
		latestBudget?.tokens.used === turnUsage,
		JSON.stringify(restored) === JSON.stringify(latestBudget),
	];
	assert.ok(
		telemetryEvidence.every(Boolean),
		`budget telemetry did not preserve reservations, denial, native usage, or crash-safe replay; evidence=${telemetryEvidence.join(",")} budgetEvents=${budgetEvents.length} turnUsage=${turnUsage} latestTokens=${latestBudget?.tokens.used}`,
	);
	writeFileSync(telemetryOutput, readFileSync(telemetryPath));
	console.log(
		`synth-demo: Phase 3 BATS PASS contexts=${contexts.length} budget_events=${budgetEvents.length} denied=2`,
	);
} finally {
	if (!shutdownEmitted) {
		await emitSessionShutdownEvent(harness.session.extensionRunner, {
			type: "session_shutdown",
			reason: "quit",
		}).catch(() => undefined);
	}
	harness.cleanup();
}
