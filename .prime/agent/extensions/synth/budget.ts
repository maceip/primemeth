import { existsSync, readFileSync } from "node:fs";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { Usage } from "@earendil-works/pi-ai";
import { isToolCallEventType, type ToolCallEvent } from "@earendil-works/pi-coding-agent";
import { Value } from "typebox/value";
import type { SynthBudgetConfig } from "./config.js";
import {
	type BudgetSnapshot,
	BudgetSnapshotSchema,
	TelemetryEventSchema,
} from "./telemetry.js";

export type BudgetRegime = "HIGH" | "MEDIUM" | "LOW" | "CRITICAL";
export type BudgetKey = "bash" | "edit" | "ipython" | "subagents" | "tokens" | "costUsd";
type ToolBudgetKey = Exclude<BudgetKey, "tokens" | "costUsd">;

interface BudgetCharge {
	key: ToolBudgetKey;
	amount: number;
}

export interface BudgetUpdate {
	before: BudgetSnapshot;
	after: BudgetSnapshot;
	regimeChanged: boolean;
}

export interface BudgetReservation extends BudgetUpdate {
	allowed: boolean;
	charges: readonly BudgetCharge[];
	reason?: string;
}

const BUDGET_KEYS: readonly BudgetKey[] = ["bash", "edit", "ipython", "subagents", "tokens", "costUsd"];
const TOOL_BUDGET_KEYS: readonly ToolBudgetKey[] = ["bash", "edit", "ipython", "subagents"];
const SUBAGENT_TOOL_NAMES = new Set(["create_subagent", "rlm", "rlm.run", "spawn_agent", "subagent"]);
const BATS_SECTION_START = "<!-- synth:bats:start -->";
const BATS_SECTION_END = "<!-- synth:bats:end -->";

export const BATS_ELISION_MARKER = "<tool_response>\nOmitted\n</tool_response>";
export const BATS_CONTINUATION_NUDGE =
	"Wait, you still have remaining tool budget — use repository search, inspection, editing, and verification tools to make further progress before concluding.";
export const BATS_WRAP_UP_INSTRUCTION =
	"Tool budget is exhausted. Wrap up now: provide the best supported final response, summarize completed work and remaining uncertainty honestly, and do not claim verification you did not perform.";

export function batchBudgetUsedFromEnvironment(): Partial<Record<BudgetKey, number>> {
	if (!process.env.SYNTH_BATCH_RUN_ID) {
		return {};
	}
	const raw = process.env.SYNTH_BATCH_BUDGET_USED_JSON;
	if (!raw) {
		return {};
	}
	const value: unknown = JSON.parse(raw);
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		throw new Error("SYNTH_BATCH_BUDGET_USED_JSON must be an object");
	}
	const record = value as Record<string, unknown>;
	if (Object.keys(record).length !== BUDGET_KEYS.length || BUDGET_KEYS.some((key) => !(key in record))) {
		throw new Error("SYNTH_BATCH_BUDGET_USED_JSON must contain every Synth budget class exactly once");
	}
	const result: Partial<Record<BudgetKey, number>> = {};
	for (const key of BUDGET_KEYS) {
		const candidate = record[key];
		if (typeof candidate !== "number" || !Number.isFinite(candidate) || candidate < 0) {
			throw new Error(`SYNTH_BATCH_BUDGET_USED_JSON.${key} must be a nonnegative finite number`);
		}
		result[key] = candidate;
	}
	return result;
}

export const BATS_SYSTEM_PROMPT_SECTION = `${BATS_SECTION_START}
## Budget-aware execution (BATS)

You have independent budgets for shell calls, edits, IPython calls, subagent spawns, native provider tokens, and native provider cost. A limit of 0 means unlimited. After each tool response, a \`<budget>\` block shows Used and Remaining values. Adapt your strategy dynamically to the current budget state.

### HIGH Budget (≥70% remaining)
- Explore broadly: batch complementary repository searches and inspect the highest-value files.
- Establish likely causes, acceptance criteria, and the verification surface before editing.
- Goal: Build context quickly without repeating equivalent reads.

### MEDIUM Budget (30%-70%)
- Narrow to the strongest hypotheses and use targeted reads or edits.
- Start focused verification as soon as a viable change exists.
- Goal: Converge and eliminate uncertainty efficiently.

### LOW Budget (10%-30%)
- Choose one tightly focused read, edit, or verification action at a time.
- Avoid speculative exploration and protect enough budget to validate the result.
- Goal: Verify the critical behavior and prepare an honest conclusion.

### CRITICAL Budget (<10% remaining or 0 in any bounded budget)
- Avoid depleted tool classes.
- Perform only one minimal-cost remaining action when it is essential.
- If no useful tool action remains, wrap up and report the state honestly.

## \`<plan>\` discipline

Maintain a dynamic tree-structured checklist of actionable steps inside \`<plan>\` tags.
- Mark each step with its status: [ ] pending, [x] done, [!] failed, [~] partial.
- Use numbered branches (1.1, 1.2) to represent alternative paths or candidate causes.
- Log resource usage after execution: (bash=#, edit=#, ipython=#, subagents=#).
- Keep all executed steps; never delete them—retain history to avoid repeats.
- Update dynamically as you reason and gather evidence, adding or revising steps as needed.
- Always consider current and remaining budget when updating the plan.
${BATS_SECTION_END}`;

function emptyUsed(): Record<BudgetKey, number> {
	return { bash: 0, edit: 0, ipython: 0, subagents: 0, tokens: 0, costUsd: 0 };
}

function limits(config: SynthBudgetConfig): Record<BudgetKey, number> {
	return {
		bash: config.bash,
		edit: config.edit,
		ipython: config.ipython,
		subagents: config.subagents,
		tokens: config.tokens,
		costUsd: config.costUsd,
	};
}

function normalizedUsed(value: Partial<Record<BudgetKey, number>>): Record<BudgetKey, number> {
	const result = emptyUsed();
	for (const key of BUDGET_KEYS) {
		const candidate = value[key];
		if (candidate !== undefined && Number.isFinite(candidate) && candidate >= 0) {
			result[key] = candidate;
		}
	}
	return result;
}

function counter(limit: number, used: number): BudgetSnapshot["bash"] {
	const unlimited = limit === 0;
	return {
		limit,
		used,
		remaining: unlimited ? null : Math.max(0, limit - used),
		unlimited,
	};
}

export function regimeForFractions(fractions: readonly number[]): BudgetRegime {
	if (fractions.length === 0) {
		return "HIGH";
	}
	const minimum = Math.min(...fractions);
	if (minimum <= 0) {
		return "CRITICAL";
	}
	if (minimum >= 0.7) {
		return "HIGH";
	}
	if (minimum >= 0.3) {
		return "MEDIUM";
	}
	if (minimum >= 0.1) {
		return "LOW";
	}
	return "CRITICAL";
}

function snapshotFor(config: SynthBudgetConfig, used: Record<BudgetKey, number>): BudgetSnapshot {
	const configuredLimits = limits(config);
	const counters = {
		bash: counter(configuredLimits.bash, used.bash),
		edit: counter(configuredLimits.edit, used.edit),
		ipython: counter(configuredLimits.ipython, used.ipython),
		subagents: counter(configuredLimits.subagents, used.subagents),
		tokens: counter(configuredLimits.tokens, used.tokens),
		costUsd: counter(configuredLimits.costUsd, used.costUsd),
	};
	const boundedFractions = BUDGET_KEYS.flatMap((key) => {
		const item = counters[key];
		return item.unlimited || item.remaining === null ? [] : [item.remaining / item.limit];
	});
	const hardExhausted =
		(!counters.tokens.unlimited && counters.tokens.remaining === 0) ||
		(!counters.costUsd.unlimited && counters.costUsd.remaining === 0);
	const boundedTools = TOOL_BUDGET_KEYS.map((key) => counters[key]).filter((item) => !item.unlimited);
	const boundedToolsExhausted =
		boundedTools.length > 0 && boundedTools.every((item) => item.remaining === 0);
	return {
		...counters,
		regime: regimeForFractions(boundedFractions),
		fullyExhausted: hardExhausted || boundedToolsExhausted,
	};
}

function budgetLabel(key: BudgetKey): string {
	switch (key) {
		case "bash":
			return "bash";
		case "edit":
			return "edit";
		case "ipython":
			return "ipython";
		case "subagents":
			return "subagent";
		case "tokens":
			return "token";
		case "costUsd":
			return "cost";
	}
}

function displayNumber(value: number): string {
	if (Number.isInteger(value)) {
		return String(value);
	}
	return value.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
}

function remainingText(item: BudgetSnapshot["bash"]): string {
	return item.remaining === null ? "unlimited" : displayNumber(item.remaining);
}

export function formatBudgetStatusLine(snapshot: BudgetSnapshot): string {
	const values = BUDGET_KEYS.map((key) => {
		const item = snapshot[key];
		return `${key} ${displayNumber(item.used)}/${item.unlimited ? "unlimited" : displayNumber(item.limit)}`;
	});
	return `[Budget: ${values.join(", ")} | Regime: ${snapshot.regime}]`;
}

export function formatBudgetBlock(snapshot: BudgetSnapshot): string {
	return (
		"\n<budget>\n" +
		`Bash Budget Used: ${displayNumber(snapshot.bash.used)}, Bash Budget Remaining: ${remainingText(snapshot.bash)}, ` +
		`Edit Budget Used: ${displayNumber(snapshot.edit.used)}, Edit Budget Remaining: ${remainingText(snapshot.edit)}, ` +
		`IPython Budget Used: ${displayNumber(snapshot.ipython.used)}, IPython Budget Remaining: ${remainingText(snapshot.ipython)}, ` +
		`Subagent Budget Used: ${displayNumber(snapshot.subagents.used)}, Subagent Budget Remaining: ${remainingText(snapshot.subagents)}, ` +
		`Token Budget Used: ${displayNumber(snapshot.tokens.used)}, Token Budget Remaining: ${remainingText(snapshot.tokens)}, ` +
		`Cost USD Budget Used: ${displayNumber(snapshot.costUsd.used)}, Cost USD Budget Remaining: ${remainingText(snapshot.costUsd)}\n` +
		"Make the best use of the available resources.\n" +
		"</budget>\n" +
		`${formatBudgetStatusLine(snapshot)}\n`
	);
}

export function injectBatsSystemPrompt(systemPrompt: string, snapshot: BudgetSnapshot): string {
	const start = systemPrompt.indexOf(BATS_SECTION_START);
	const end = systemPrompt.indexOf(BATS_SECTION_END);
	let base = systemPrompt.trimEnd();
	if (start >= 0 && end >= start) {
		base = `${systemPrompt.slice(0, start)}${systemPrompt.slice(end + BATS_SECTION_END.length)}`.trimEnd();
	}
	return `${base}\n\n${BATS_SYSTEM_PROMPT_SECTION}\n\nCurrent budget state:\n${formatBudgetStatusLine(snapshot)}`;
}

export function chargesForToolCall(event: ToolCallEvent): readonly BudgetCharge[] {
	if (isToolCallEventType("bash", event)) {
		return [{ key: "bash", amount: 1 }];
	}
	if (isToolCallEventType("edit", event)) {
		return [{ key: "edit", amount: 1 }];
	}
	if (isToolCallEventType("ipython", event)) {
		return [{ key: "ipython", amount: 1 }];
	}
	if (SUBAGENT_TOOL_NAMES.has(event.toolName)) {
		return [{ key: "subagents", amount: 1 }];
	}
	return [];
}

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
	if ("content" in message) {
		return typeof message.content === "string"
			? message.content
			: message.content.flatMap((item) => (item.type === "text" ? [item.text] : [])).join("\n");
	}
	return "";
}

export function elideOldToolResponses(
	messages: readonly AgentMessage[],
	elideAfterTurns: number,
): AgentMessage[] {
	if (elideAfterTurns === 0) {
		return [...messages];
	}
	const latestAssistantTurn = messages.reduce(
		(count, message) => count + (message.role === "assistant" ? 1 : 0),
		-1,
	);
	let assistantTurn = -1;
	return messages.map((message) => {
		if (message.role === "assistant") {
			assistantTurn++;
			return message;
		}
		if (message.role !== "toolResult" || latestAssistantTurn - assistantTurn < elideAfterTurns) {
			return message;
		}
		return {
			...message,
			content: [{ type: "text", text: BATS_ELISION_MARKER }],
		};
	});
}

export function assistantStalled(message: AgentMessage): boolean {
	if (
		message.role !== "assistant" ||
		message.stopReason === "error" ||
		message.stopReason === "aborted" ||
		message.content.some((item) => item.type === "toolCall")
	) {
		return false;
	}
	const text = messageText(message).trim().toLowerCase();
	return (
		text.length === 0 ||
		/^(?:none|i (?:do not|don't) know|i (?:cannot|can't) (?:complete|determine|finish)|unable to (?:complete|determine|finish))(?:[.!])?$/.test(
			text,
		)
	);
}

export function shouldSendContinuationNudge(
	noToolCount: number,
	noneAnswerThreshold: number,
	hasUsefulBudget: boolean,
	fullyExhausted: boolean,
): boolean {
	return noToolCount >= 1 && noToolCount < noneAnswerThreshold && hasUsefulBudget && !fullyExhausted;
}

export class BudgetTracker {
	private readonly configuredLimits: Record<BudgetKey, number>;
	private readonly used: Record<BudgetKey, number>;

	constructor(
		readonly config: SynthBudgetConfig,
		restoredUsed: Partial<Record<BudgetKey, number>> = {},
	) {
		this.configuredLimits = limits(config);
		this.used = normalizedUsed(restoredUsed);
	}

	static fromTelemetry(
		config: SynthBudgetConfig,
		path: string,
		initialUsed: Partial<Record<BudgetKey, number>> = {},
	): BudgetTracker {
		if (!existsSync(path)) {
			return new BudgetTracker(config, initialUsed);
		}
		let restored: Partial<Record<BudgetKey, number>> = {};
		const lines = readFileSync(path, "utf8")
			.split("\n")
			.filter((line) => line.trim().length > 0);
		for (const [index, line] of lines.entries()) {
			let value: unknown;
			try {
				value = JSON.parse(line);
			} catch {
				throw new Error(`budget recovery found invalid JSON at line ${index + 1}`);
			}
			if (!Value.Check(TelemetryEventSchema, value)) {
				throw new Error(`budget recovery found invalid telemetry at line ${index + 1}`);
			}
			if (value.kind !== "budget" || value.budget === undefined) {
				continue;
			}
			if (!Value.Check(BudgetSnapshotSchema, value.budget)) {
				throw new Error(`budget recovery found an invalid snapshot at line ${index + 1}`);
			}
			restored = Object.fromEntries(BUDGET_KEYS.map((key) => [key, value.budget?.[key].used]));
		}
		return new BudgetTracker(config, restored);
	}

	static exhausted(config: SynthBudgetConfig): BudgetTracker {
		const configuredLimits = limits(config);
		return new BudgetTracker(
			config,
			Object.fromEntries(
				BUDGET_KEYS.map((key) => [key, configuredLimits[key] === 0 ? 0 : configuredLimits[key]]),
			),
		);
	}

	snapshot(): BudgetSnapshot {
		return snapshotFor(this.config, this.used);
	}

	hasUsefulToolBudget(): boolean {
		const snapshot = this.snapshot();
		if (snapshot.fullyExhausted) {
			return false;
		}
		return TOOL_BUDGET_KEYS.some((key) => {
			const item = snapshot[key];
			return item.unlimited || (item.remaining ?? 0) > 0;
		});
	}

	addUsage(usage: Usage): BudgetUpdate {
		const before = this.snapshot();
		this.used.tokens += usage.totalTokens;
		this.used.costUsd += usage.cost.total;
		const after = this.snapshot();
		return { before, after, regimeChanged: before.regime !== after.regime };
	}

	reserve(event: ToolCallEvent): BudgetReservation {
		const before = this.snapshot();
		const charges = chargesForToolCall(event);
		const hardBlock = (before.tokens.remaining === 0 && !before.tokens.unlimited ? "tokens" : undefined) ??
			(before.costUsd.remaining === 0 && !before.costUsd.unlimited ? "costUsd" : undefined);
		if (hardBlock) {
			return {
				allowed: false,
				charges,
				reason: `No ${budgetLabel(hardBlock)} budget left.`,
				before,
				after: before,
				regimeChanged: false,
			};
		}
		for (const charge of charges) {
			const item = before[charge.key];
			if (!item.unlimited && (item.remaining ?? 0) < charge.amount) {
				return {
					allowed: false,
					charges,
					reason: `No ${budgetLabel(charge.key)} budget left.`,
					before,
					after: before,
					regimeChanged: false,
				};
			}
		}
		for (const charge of charges) {
			this.used[charge.key] += charge.amount;
		}
		const after = this.snapshot();
		return {
			allowed: true,
			charges,
			before,
			after,
			regimeChanged: before.regime !== after.regime,
		};
	}
}
