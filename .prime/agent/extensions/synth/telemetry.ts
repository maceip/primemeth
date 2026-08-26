import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { Usage } from "@earendil-works/pi-ai";
import { type Static, Type } from "typebox";
import { Value } from "typebox/value";

export const TELEMETRY_SCHEMA_VERSION = 1;

const CostSchema = Type.Object(
	{
		input: Type.Number({ minimum: 0 }),
		output: Type.Number({ minimum: 0 }),
		cacheRead: Type.Number({ minimum: 0 }),
		cacheWrite: Type.Number({ minimum: 0 }),
		total: Type.Number({ minimum: 0 }),
	},
	{ additionalProperties: false },
);

export const UsageSchema = Type.Object(
	{
		input: Type.Integer({ minimum: 0 }),
		output: Type.Integer({ minimum: 0 }),
		cacheRead: Type.Integer({ minimum: 0 }),
		cacheWrite: Type.Integer({ minimum: 0 }),
		totalTokens: Type.Integer({ minimum: 0 }),
		cost: CostSchema,
	},
	{ additionalProperties: false },
);

const TranscriptPointerSchema = Type.Object(
	{
		sessionFile: Type.String({ minLength: 1 }),
		entryId: Type.Union([Type.String(), Type.Null()]),
	},
	{ additionalProperties: false },
);

export const TerminationReasonSchema = Type.Union([
	Type.Literal("SUCCESS"),
	Type.Literal("FAILED_ASSERTION"),
	Type.Literal("TIMEOUT"),
	Type.Literal("BUDGET_EXHAUSTED"),
	Type.Literal("TOOL_ERROR"),
	Type.Literal("UNKNOWN"),
]);

const OutcomeEvidenceSchema = Type.Object(
	{
		kind: Type.Union([Type.Literal("transcript"), Type.Literal("artifact")]),
		uri: Type.String({ minLength: 1 }),
		entry_id: Type.Union([Type.String({ minLength: 1 }), Type.Null()]),
		sha256: Type.Optional(Type.String({ pattern: "^[a-f0-9]{64}$" })),
	},
	{ additionalProperties: false },
);

export const TerminalOutcomeSchema = Type.Object(
	{
		reward: Type.Union([Type.Literal(0), Type.Literal(1), Type.Null()]),
		termination_reason: TerminationReasonSchema,
		evidence: OutcomeEvidenceSchema,
	},
	{ additionalProperties: false },
);

const OutcomeSignalsSchema = Type.Object(
	{
		agent_exit_code: Type.Integer(),
		agent_timed_out: Type.Boolean(),
		acceptance_configured: Type.Boolean(),
		acceptance_exit_code: Type.Union([Type.Integer(), Type.Null()]),
		acceptance_timed_out: Type.Boolean(),
		acceptance_integrity_failures: Type.Array(Type.String({ minLength: 1 })),
	},
	{ additionalProperties: false },
);

export const TerminalOutcomeReceiptSchema = Type.Object(
	{
		schema_version: Type.Literal(1),
		run_id: Type.String({ pattern: "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" }),
		trajectory_index: Type.Integer({ minimum: 1 }),
		session_id: Type.Union([Type.String({ minLength: 1 }), Type.Null()]),
		outcome: TerminalOutcomeSchema,
		signals: OutcomeSignalsSchema,
	},
	{ additionalProperties: false },
);

export type TerminationReason = Static<typeof TerminationReasonSchema>;
export type TerminalOutcome = Static<typeof TerminalOutcomeSchema>;
export type TerminalOutcomeReceipt = Static<typeof TerminalOutcomeReceiptSchema>;

const ToolExchangeSchema = Type.Object(
	{
		toolCallId: Type.String({ minLength: 1 }),
		name: Type.String({ minLength: 1 }),
		argsDigest: Type.String({ pattern: "^[a-f0-9]{64}$" }),
		isError: Type.Boolean(),
	},
	{ additionalProperties: false },
);

const ProviderExchangeSchema = Type.Object(
	{
		requestId: Type.String({ minLength: 1 }),
		payloadDigest: Type.String({ pattern: "^[a-f0-9]{64}$" }),
		provider: Type.Union([Type.String(), Type.Null()]),
		model: Type.Union([Type.String(), Type.Null()]),
		status: Type.Integer({ minimum: 0 }),
		ttftMs: Type.Optional(Type.Number({ minimum: 0 })),
		generationMs: Type.Optional(Type.Number({ minimum: 0 })),
		prefillTokens: Type.Optional(Type.Integer({ minimum: 0 })),
		decodeTokens: Type.Optional(Type.Integer({ minimum: 0 })),
		prefillTokensPerSecond: Type.Optional(Type.Number({ minimum: 0 })),
		decodeTokensPerSecond: Type.Optional(Type.Number({ minimum: 0 })),
	},
	{ additionalProperties: false },
);

const BudgetCounterSchema = Type.Object(
	{
		limit: Type.Number({ minimum: 0 }),
		used: Type.Number({ minimum: 0 }),
		remaining: Type.Union([Type.Number({ minimum: 0 }), Type.Null()]),
		unlimited: Type.Boolean(),
	},
	{ additionalProperties: false },
);

export const BudgetSnapshotSchema = Type.Object(
	{
		bash: BudgetCounterSchema,
		edit: BudgetCounterSchema,
		ipython: BudgetCounterSchema,
		subagents: BudgetCounterSchema,
		tokens: BudgetCounterSchema,
		costUsd: BudgetCounterSchema,
		regime: Type.Union([
			Type.Literal("HIGH"),
			Type.Literal("MEDIUM"),
			Type.Literal("LOW"),
			Type.Literal("CRITICAL"),
		]),
		fullyExhausted: Type.Boolean(),
	},
	{ additionalProperties: false },
);

export type BudgetSnapshot = Static<typeof BudgetSnapshotSchema>;

const TelemetryKindSchema = Type.Union([
	Type.Literal("session.start"),
	Type.Literal("session.shutdown"),
	Type.Literal("session.outcome"),
	Type.Literal("turn"),
	Type.Literal("tool"),
	Type.Literal("provider"),
	Type.Literal("agent.end"),
	Type.Literal("bridge"),
	Type.Literal("budget"),
]);

export const TelemetryEventSchema = Type.Object(
	{
		schemaVersion: Type.Literal(TELEMETRY_SCHEMA_VERSION),
		eventId: Type.String({ minLength: 1 }),
		sequence: Type.Integer({ minimum: 1 }),
		timestamp: Type.String({ minLength: 1 }),
		sessionId: Type.String({ minLength: 1 }),
		kind: TelemetryKindSchema,
		turnIndex: Type.Optional(Type.Integer({ minimum: 0 })),
		durationMs: Type.Optional(Type.Number({ minimum: 0 })),
		usage: Type.Optional(UsageSchema),
		tool: Type.Optional(ToolExchangeSchema),
		provider: Type.Optional(ProviderExchangeSchema),
		budget: Type.Optional(BudgetSnapshotSchema),
		outcome: Type.Optional(TerminalOutcomeSchema),
		transcript: Type.Optional(TranscriptPointerSchema),
		errorClass: Type.Optional(Type.String()),
		data: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
	},
	{ additionalProperties: false },
);

export type TelemetryEvent = Static<typeof TelemetryEventSchema>;
export type TelemetryEventInput = Omit<TelemetryEvent, "eventId" | "schemaVersion" | "sequence" | "timestamp">;

function terminalOutcomeError(outcome: TerminalOutcome): string | undefined {
	if (outcome.termination_reason === "SUCCESS" && outcome.reward !== 1) {
		return "SUCCESS requires reward 1";
	}
	if (outcome.reward === 1 && outcome.termination_reason !== "SUCCESS") {
		return "reward 1 requires SUCCESS";
	}
	if (
		(outcome.termination_reason === "FAILED_ASSERTION" || outcome.termination_reason === "TIMEOUT") &&
		outcome.reward !== 0
	) {
		return `${outcome.termination_reason} requires reward 0`;
	}
	if (outcome.termination_reason === "BUDGET_EXHAUSTED" && outcome.reward !== null) {
		return "BUDGET_EXHAUSTED requires a null reward";
	}
	if (outcome.termination_reason === "UNKNOWN" && outcome.reward !== null) {
		return "UNKNOWN requires a null reward";
	}
	return undefined;
}

function telemetryEventError(event: TelemetryEvent): string | undefined {
	if (event.kind === "budget" && event.budget === undefined) {
		return "budget telemetry event requires a snapshot";
	}
	if (event.kind === "session.outcome") {
		if (event.outcome === undefined) return "session.outcome telemetry event requires an outcome";
		return terminalOutcomeError(event.outcome);
	}
	if (event.outcome !== undefined) {
		return "only session.outcome telemetry events may contain an outcome";
	}
	return undefined;
}

export function parseTerminalOutcomeReceipt(value: unknown): TerminalOutcomeReceipt {
	if (!Value.Check(TerminalOutcomeReceiptSchema, value)) {
		throw new Error("terminal outcome receipt failed its TypeBox schema");
	}
	const semanticError = terminalOutcomeError(value.outcome);
	if (semanticError) throw new Error(`terminal outcome receipt is invalid: ${semanticError}`);
	return value;
}

function canonicalize(value: unknown): unknown {
	if (Array.isArray(value)) {
		return value.map(canonicalize);
	}
	if (value && typeof value === "object") {
		const record = value as Record<string, unknown>;
		return Object.fromEntries(Object.keys(record).sort().map((key) => [key, canonicalize(record[key])]));
	}
	if (typeof value === "bigint") {
		return value.toString();
	}
	if (typeof value === "function" || typeof value === "symbol" || value === undefined) {
		return String(value);
	}
	return value;
}

export function digestValue(value: unknown): string {
	return createHash("sha256").update(JSON.stringify(canonicalize(value))).digest("hex");
}

export function copyUsage(usage: Usage): Static<typeof UsageSchema> {
	return {
		input: usage.input,
		output: usage.output,
		cacheRead: usage.cacheRead,
		cacheWrite: usage.cacheWrite,
		totalTokens: usage.totalTokens,
		cost: {
			input: usage.cost.input,
			output: usage.cost.output,
			cacheRead: usage.cost.cacheRead,
			cacheWrite: usage.cost.cacheWrite,
			total: usage.cost.total,
		},
	};
}

export function sumUsage(usages: readonly Usage[]): Static<typeof UsageSchema> | undefined {
	if (usages.length === 0) {
		return undefined;
	}
	const total: Static<typeof UsageSchema> = {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
	for (const usage of usages) {
		total.input += usage.input;
		total.output += usage.output;
		total.cacheRead += usage.cacheRead;
		total.cacheWrite += usage.cacheWrite;
		total.totalTokens += usage.totalTokens;
		total.cost.input += usage.cost.input;
		total.cost.output += usage.cost.output;
		total.cost.cacheRead += usage.cost.cacheRead;
		total.cost.cacheWrite += usage.cost.cacheWrite;
		total.cost.total += usage.cost.total;
	}
	return total;
}

interface PriorTelemetryState {
	sequence: number;
	terminalOutcomeWritten: boolean;
}

function priorTelemetryState(path: string, sessionId: string): PriorTelemetryState {
	if (!existsSync(path)) {
		return { sequence: 0, terminalOutcomeWritten: false };
	}
	const lines = readFileSync(path, "utf8")
		.split("\n")
		.filter((line) => line.trim().length > 0);
	let sequence = 0;
	let terminalOutcomeWritten = false;
	try {
		for (const line of lines) {
			const value: unknown = JSON.parse(line);
			if (!Value.Check(TelemetryEventSchema, value)) {
				throw new Error("event does not match the telemetry schema");
			}
			const semanticError = telemetryEventError(value);
			if (semanticError) throw new Error(semanticError);
			if (value.sessionId !== sessionId) {
				throw new Error(`event belongs to session ${value.sessionId}`);
			}
			if (value.sequence !== sequence + 1) {
				throw new Error(`expected sequence ${sequence + 1}, got ${value.sequence}`);
			}
			sequence = value.sequence;
			if (value.kind === "session.outcome") {
				if (terminalOutcomeWritten) throw new Error("session has multiple terminal outcomes");
				terminalOutcomeWritten = true;
			}
		}
		return { sequence, terminalOutcomeWritten };
	} catch (error) {
		const detail = error instanceof Error ? error.message : String(error);
		throw new Error(`cannot resume telemetry file ${path}: ${detail}`);
	}
}

export class TelemetryWriter {
	readonly path: string;
	private sequence: number;
	private terminalOutcomeWritten: boolean;

	constructor(directory: string, private readonly sessionId: string) {
		mkdirSync(directory, { recursive: true });
		const safeSessionId = sessionId.replace(/[^A-Za-z0-9._-]/g, "_");
		this.path = join(directory, `${safeSessionId}.jsonl`);
		const prior = priorTelemetryState(this.path, sessionId);
		this.sequence = prior.sequence;
		this.terminalOutcomeWritten = prior.terminalOutcomeWritten;
	}

	hasTerminalOutcome(): boolean {
		return this.terminalOutcomeWritten;
	}

	append(input: TelemetryEventInput): TelemetryEvent {
		const candidate = {
			...input,
			schemaVersion: TELEMETRY_SCHEMA_VERSION,
			eventId: randomUUID(),
			sequence: ++this.sequence,
			timestamp: new Date().toISOString(),
		};
		const event = Object.fromEntries(
			Object.entries(candidate).filter(([, value]) => value !== undefined),
		) as TelemetryEvent;
		if (!Value.Check(TelemetryEventSchema, event)) {
			this.sequence--;
			throw new Error("telemetry event failed its TypeBox schema");
		}
		const semanticError = telemetryEventError(event);
		if (semanticError) {
			this.sequence--;
			throw new Error(semanticError);
		}
		if (event.kind === "session.outcome" && this.terminalOutcomeWritten) {
			this.sequence--;
			throw new Error("session telemetry already contains a terminal outcome");
		}
		appendFileSync(this.path, `${JSON.stringify(event)}\n`, "utf8");
		if (event.kind === "session.outcome") this.terminalOutcomeWritten = true;
		return event;
	}
}
