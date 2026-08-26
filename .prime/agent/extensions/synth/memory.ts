export const REASONING_BANK_TASK_DESCRIPTION =
	"Given prior software engineering memories, analyze the current task's intent and select relevant strategies or lessons that could help resolve it.";

export const LESSONS_HEADING = "## Lessons from past sessions";
export const PROBLEM_FRAMING_HEADING = "## Problem framing";
const LESSONS_PREAMBLE =
	"These lessons were auto-distilled from prior sessions and are distinct from user-curated HarnessState memory. Use them only when relevant.";
const PROBLEM_FRAMING_PREAMBLE =
	"These contrastive heuristics were induced from a failed session and similar successful memory. Use them to frame the problem before selecting implementation steps.";

export interface MemoryItem {
	id: number;
	title: string;
	description: string;
	content: string;
	status: "success" | "fail";
	itemType: "strategy" | "meta_heuristic";
	taskQuery: string;
	sessionId: string;
	templateKey: string | null;
	createdAt: string;
	score?: number;
}

export interface PersistedMemoryRetrieval {
	taskQuery: string;
	section?: string;
	itemIds: number[];
	framingSection?: string;
	framingItemIds: number[];
	timestamp: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonemptyString(value: unknown, field: string): string {
	if (typeof value !== "string" || value.length === 0) {
		throw new Error(`memory response ${field} must be a nonempty string`);
	}
	return value;
}

function parseItem(value: unknown, scoreRequired: boolean): MemoryItem {
	if (!isRecord(value)) {
		throw new Error("memory response item must be an object");
	}
	if (!Number.isSafeInteger(value.id) || Number(value.id) <= 0) {
		throw new Error("memory response item.id must be a positive integer");
	}
	if (value.status !== "success" && value.status !== "fail") {
		throw new Error("memory response item.status is invalid");
	}
	if (value.itemType !== "strategy" && value.itemType !== "meta_heuristic") {
		throw new Error("memory response item.itemType is invalid");
	}
	if (value.templateKey !== null && typeof value.templateKey !== "string") {
		throw new Error("memory response item.templateKey must be a string or null");
	}
	if (
		value.score !== undefined &&
		(typeof value.score !== "number" || !Number.isFinite(value.score) || value.score < -1 || value.score > 1)
	) {
		throw new Error("memory response item.score must be between -1 and 1");
	}
	if (scoreRequired && value.score === undefined) {
		throw new Error("memory query response item omitted score");
	}
	return {
		id: Number(value.id),
		title: nonemptyString(value.title, "item.title"),
		description: nonemptyString(value.description, "item.description"),
		content: nonemptyString(value.content, "item.content"),
		status: value.status,
		itemType: value.itemType,
		taskQuery: nonemptyString(value.taskQuery, "item.taskQuery"),
		sessionId: nonemptyString(value.sessionId, "item.sessionId"),
		templateKey: value.templateKey,
		createdAt: nonemptyString(value.createdAt, "item.createdAt"),
		...(value.score === undefined ? {} : { score: value.score }),
	};
}

export function parseMemoryItems(value: unknown, scoreRequired: boolean): MemoryItem[] {
	if (!isRecord(value) || !Array.isArray(value.items)) {
		throw new Error("memory response must contain an items array");
	}
	return value.items.map((item) => parseItem(item, scoreRequired));
}

export function renderLessons(items: readonly MemoryItem[]): string | undefined {
	const strategies = items.filter((item) => item.itemType === "strategy");
	if (strategies.length === 0) {
		return undefined;
	}
	const rendered = strategies.map(
		(item, index) =>
			`### Memory Item ${index + 1}\nStatus: ${item.status}\nTitle: ${item.title}\nDescription: ${item.description}\nContent: ${item.content}`,
	);
	return `${LESSONS_HEADING}\n${LESSONS_PREAMBLE}\n\n${rendered.join("\n\n")}`;
}

export function renderProblemFraming(items: readonly MemoryItem[]): string | undefined {
	const heuristics = items.filter((item) => item.itemType === "meta_heuristic").slice(0, 1);
	if (heuristics.length === 0) return undefined;
	const rendered = heuristics.map(
		(item) =>
			`### Framing Heuristic\nStatus: ${item.status}\nTitle: ${item.title}\nDescription: ${item.description}\nContent: ${item.content}`,
	);
	return `${PROBLEM_FRAMING_HEADING}\n${PROBLEM_FRAMING_PREAMBLE}\n\n${rendered.join("\n\n")}`;
}

export function renderMemoryList(items: readonly MemoryItem[]): string {
	if (items.length === 0) {
		return "Synth memory list\nNo stored memories.";
	}
	return `Synth memory list\n${items
		.map(
			(item) =>
				`#${item.id} [${item.status}/${item.itemType}] ${item.title}\n${item.description}\nSession: ${item.sessionId}`,
		)
		.join("\n\n")}`;
}

export function renderMemorySearch(query: string, items: readonly MemoryItem[]): string {
	if (items.length === 0) {
		return `Synth memory search: ${query}\nNo relevant memories.`;
	}
	return `Synth memory search: ${query}\n${items
		.map(
			(item) =>
				`#${item.id} score=${item.score?.toFixed(3) ?? "n/a"} [${item.status}] ${item.title}\n${item.description}\n${item.content}`,
		)
		.join("\n\n")}`;
}

export function parsePersistedMemoryRetrieval(value: unknown): PersistedMemoryRetrieval | undefined {
	if (
		!isRecord(value) ||
		typeof value.taskQuery !== "string" ||
		!value.taskQuery ||
		typeof value.timestamp !== "number" ||
		!Number.isFinite(value.timestamp)
	) {
		return undefined;
	}
	const section =
		typeof value.section === "string" && value.section.startsWith(LESSONS_HEADING)
			? value.section
			: undefined;
	const framingSection =
		typeof value.framingSection === "string" && value.framingSection.startsWith(PROBLEM_FRAMING_HEADING)
			? value.framingSection
			: undefined;
	if (!Array.isArray(value.itemIds)) return undefined;
	if (value.framingItemIds !== undefined && !Array.isArray(value.framingItemIds)) return undefined;
	const itemIds = value.itemIds;
	const framingItemIds = value.framingItemIds ?? [];
	if (
		(!section && !framingSection) ||
		!itemIds.every((id) => Number.isSafeInteger(id) && Number(id) > 0) ||
		!framingItemIds.every((id) => Number.isSafeInteger(id) && Number(id) > 0) ||
		Boolean(section) !== (itemIds.length > 0) ||
		Boolean(framingSection) !== (framingItemIds.length > 0)
	) {
		return undefined;
	}
	return {
		taskQuery: value.taskQuery,
		...(section ? { section } : {}),
		itemIds: itemIds.map(Number),
		...(framingSection ? { framingSection } : {}),
		framingItemIds: framingItemIds.map(Number),
		timestamp: value.timestamp,
	};
}
