import { randomUUID } from "node:crypto";
import { type Static, Type } from "typebox";

export const COMPILE_MARKER_PREFIX = "SYNTH_SKILL_COMPILE_V1:";

const NAME_PATTERN = "^[a-z0-9]+(?:-[a-z0-9]+)*$";
const REQUEST_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export const CompileSkillParameters = Type.Object(
	{
		workflow: Type.String({ minLength: 20, maxLength: 8000 }),
		nameHint: Type.Optional(Type.String({ minLength: 1, maxLength: 64, pattern: NAME_PATTERN })),
		reason: Type.String({ minLength: 1, maxLength: 1000 }),
	},
	{ additionalProperties: false },
);

export type CompileSkillInput = Static<typeof CompileSkillParameters>;
export type CompileSource = "command" | "kernel" | "tool";
export type CompileStatus = "scheduled" | "started" | "promoted" | "rejected" | "failed";

export interface CompileMarker {
	requestId?: string;
	workflow?: string;
	nameHint?: string;
	reason: string;
	source: CompileSource;
}

export interface CompileRequest extends CompileMarker {
	requestId: string;
	sessionFile: string;
	sessionId: string;
	timestamp: number;
}

export interface PersistedCompile extends CompileRequest {
	status: CompileStatus;
	name?: string;
	contentHash?: string;
	error?: string;
	pid?: number;
	jobOutput?: string;
	jobError?: string;
}

export interface CompilerOutcome {
	requestId: string;
	sessionId: string;
	name: string | null;
	status: "promoted" | "rejected";
	contentHash: string | null;
	destination: string | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, required: readonly string[], optional: readonly string[] = []): boolean {
	const allowed = new Set([...required, ...optional]);
	return required.every((key) => key in value) && Object.keys(value).every((key) => allowed.has(key));
}

function validName(value: unknown): value is string {
	return typeof value === "string" && value.length <= 64 && new RegExp(NAME_PATTERN).test(value);
}

function validWorkflow(value: unknown): value is string {
	return typeof value === "string" && value.trim().length >= 20 && value.length <= 8000;
}

function validReason(value: unknown): value is string {
	return typeof value === "string" && value.trim().length > 0 && value.length <= 1000;
}

function validRequestId(value: unknown): value is string {
	return typeof value === "string" && REQUEST_ID_PATTERN.test(value);
}

export function parseCompileMarker(value: string | undefined): CompileMarker | undefined {
	if (!value?.startsWith(COMPILE_MARKER_PREFIX)) return undefined;
	let parsed: unknown;
	try {
		parsed = JSON.parse(value.slice(COMPILE_MARKER_PREFIX.length));
	} catch {
		throw new Error("Synth compile marker is not valid JSON");
	}
	if (!isRecord(parsed) || !exactKeys(parsed, ["reason", "source"], ["requestId", "workflow", "nameHint"])) {
		throw new Error("Synth compile marker has an invalid field set");
	}
	if (parsed.source !== "command" && parsed.source !== "kernel" && parsed.source !== "tool") {
		throw new Error("Synth compile marker source is invalid");
	}
	if (!validReason(parsed.reason)) throw new Error("Synth compile marker reason is invalid");
	if (parsed.requestId !== undefined && !validRequestId(parsed.requestId)) {
		throw new Error("Synth compile marker requestId is invalid");
	}
	if (parsed.workflow !== undefined && !validWorkflow(parsed.workflow)) {
		throw new Error("Synth compile marker workflow is invalid");
	}
	if (parsed.nameHint !== undefined && !validName(parsed.nameHint)) {
		throw new Error("Synth compile marker nameHint is invalid");
	}
	return {
		...(parsed.requestId === undefined ? {} : { requestId: parsed.requestId }),
		...(parsed.workflow === undefined ? {} : { workflow: parsed.workflow.trim() }),
		...(parsed.nameHint === undefined ? {} : { nameHint: parsed.nameHint }),
		reason: parsed.reason.trim(),
		source: parsed.source,
	};
}

export function createCompileRequest(
	marker: CompileMarker,
	sessionFile: string,
	sessionId: string,
): CompileRequest {
	return {
		...marker,
		requestId: marker.requestId ?? randomUUID(),
		sessionFile,
		sessionId,
		timestamp: Date.now(),
	};
}

export function buildCompileMarker(request: CompileRequest): string {
	return `${COMPILE_MARKER_PREFIX}${JSON.stringify({
		requestId: request.requestId,
		...(request.workflow === undefined ? {} : { workflow: request.workflow }),
		...(request.nameHint === undefined ? {} : { nameHint: request.nameHint }),
		reason: request.reason,
		source: request.source,
	})}`;
}

export function persistCompile(request: CompileRequest, status: CompileStatus, detail: Partial<PersistedCompile> = {}): PersistedCompile {
	return { ...request, ...detail, status };
}

export function parsePersistedCompile(value: unknown): PersistedCompile | undefined {
	if (
		!isRecord(value) ||
		!exactKeys(
			value,
			["requestId", "reason", "sessionFile", "sessionId", "source", "status", "timestamp"],
			["workflow", "nameHint", "name", "contentHash", "error", "pid", "jobOutput", "jobError"],
		) ||
		!validRequestId(value.requestId) ||
		!validReason(value.reason) ||
		typeof value.sessionFile !== "string" ||
		typeof value.sessionId !== "string" ||
		(value.source !== "command" && value.source !== "kernel" && value.source !== "tool") ||
		!(["scheduled", "started", "promoted", "rejected", "failed"] as const).includes(
			value.status as CompileStatus,
		) ||
		typeof value.timestamp !== "number" ||
		!Number.isFinite(value.timestamp) ||
		(value.workflow !== undefined && !validWorkflow(value.workflow)) ||
		(value.nameHint !== undefined && !validName(value.nameHint)) ||
		(value.name !== undefined && !validName(value.name)) ||
		(value.contentHash !== undefined && typeof value.contentHash !== "string") ||
		(value.error !== undefined && typeof value.error !== "string") ||
		(value.pid !== undefined && (!Number.isSafeInteger(value.pid) || Number(value.pid) <= 1)) ||
		(value.jobOutput !== undefined && typeof value.jobOutput !== "string") ||
		(value.jobError !== undefined && typeof value.jobError !== "string")
	) {
		return undefined;
	}
	return value as unknown as PersistedCompile;
}

export function parseCompilerOutcome(stdout: string): CompilerOutcome {
	let value: unknown;
	try {
		value = JSON.parse(stdout);
	} catch {
		throw new Error("Synth compiler returned invalid JSON");
	}
	if (
		!isRecord(value) ||
		typeof value.requestId !== "string" ||
		typeof value.sessionId !== "string" ||
		(value.status !== "promoted" && value.status !== "rejected") ||
		(value.name !== null && !validName(value.name)) ||
		(value.contentHash !== null && typeof value.contentHash !== "string") ||
		(value.destination !== null && typeof value.destination !== "string")
	) {
		throw new Error("Synth compiler outcome is invalid");
	}
	return {
		requestId: value.requestId,
		sessionId: value.sessionId,
		name: value.name,
		status: value.status,
		contentHash: value.contentHash,
		destination: value.destination,
	};
}

export function compilerArguments(workspace: string, request: CompileRequest, fixturePath?: string): string[] {
	return [
		"run",
		"--project",
		workspace,
		"--no-sync",
		"synth",
		"compile",
		"--from",
		request.sessionFile,
		"--request-id",
		request.requestId,
		"--actor",
		`synth-extension:${request.source}`,
		"--reason",
		request.reason,
		...(request.workflow === undefined ? [] : ["--workflow", request.workflow]),
		...(request.nameHint === undefined ? [] : ["--name-hint", request.nameHint]),
		...(fixturePath ? ["--fixture", fixturePath] : []),
	];
}
