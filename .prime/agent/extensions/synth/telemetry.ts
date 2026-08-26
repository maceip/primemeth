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
	},
	{ additionalProperties: false },
);

const TelemetryKindSchema = Type.Union([
	Type.Literal("session.start"),
	Type.Literal("session.shutdown"),
	Type.Literal("turn"),
	Type.Literal("tool"),
	Type.Literal("provider"),
	Type.Literal("agent.end"),
	Type.Literal("bridge"),
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
		transcript: Type.Optional(TranscriptPointerSchema),
		errorClass: Type.Optional(Type.String()),
		data: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
	},
	{ additionalProperties: false },
);

export type TelemetryEvent = Static<typeof TelemetryEventSchema>;
export type TelemetryEventInput = Omit<TelemetryEvent, "eventId" | "schemaVersion" | "sequence" | "timestamp">;

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

function priorSequence(path: string, sessionId: string): number {
	if (!existsSync(path)) {
		return 0;
	}
	const lines = readFileSync(path, "utf8")
		.split("\n")
		.filter((line) => line.trim().length > 0);
	const lastLine = lines.at(-1);
	if (!lastLine) {
		return 0;
	}
	try {
		const value: unknown = JSON.parse(lastLine);
		if (!Value.Check(TelemetryEventSchema, value)) {
			throw new Error("last event does not match the telemetry schema");
		}
		if (value.sessionId !== sessionId) {
			throw new Error(`last event belongs to session ${value.sessionId}`);
		}
		return value.sequence;
	} catch (error) {
		const detail = error instanceof Error ? error.message : String(error);
		throw new Error(`cannot resume telemetry file ${path}: ${detail}`);
	}
}

export class TelemetryWriter {
	readonly path: string;
	private sequence: number;

	constructor(directory: string, private readonly sessionId: string) {
		mkdirSync(directory, { recursive: true });
		const safeSessionId = sessionId.replace(/[^A-Za-z0-9._-]/g, "_");
		this.path = join(directory, `${safeSessionId}.jsonl`);
		this.sequence = priorSequence(this.path, sessionId);
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
		appendFileSync(this.path, `${JSON.stringify(event)}\n`, "utf8");
		return event;
	}
}
