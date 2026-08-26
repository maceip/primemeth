import { readFileSync } from "node:fs";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import type { AssistantMessage } from "@earendil-works/pi-ai";
import type { SessionEntry } from "@earendil-works/pi-coding-agent";
import type { SynthConfig } from "./config.js";
import type { BudgetSnapshot } from "./telemetry.js";
import { formatBudgetBlock } from "./budget.js";

export type VerificationDecision = "SUCCESS" | "CONTINUE" | "PIVOT";

export interface VerificationResult {
	verification: string;
	decision: VerificationDecision;
	justification: string;
	trajectorySummary: string;
	details: Record<string, unknown>;
}

export interface PersistedVerification extends VerificationResult {
	assistantEntryId: string;
	cycle: number;
	mode: "interactive" | "batch";
	task: string;
	timestamp: number;
}

const VERIFICATION_KEYS = new Set([
	"verification",
	"decision",
	"justification",
	"trajectory_summary",
	"details",
]);
const FIXTURE_KEYS = new Set(["verifications", "compaction"]);
const MODEL_TIMEOUT_MS = 30_000;
const MAX_TRAJECTORY_CHARS = 60_000;
const MAX_EVIDENCE_CHARS = 12_000;

// Source: budget-aware-agent/src/prompts_bats.py:322-386 at 37d23f6.
// Adapted only from web-question terminology to coding-task evidence.
export const BATS_CODING_VERIFIER_INSTRUCTION = `Now you are an AI Strategic Verifier. Your primary goal is to evaluate the above answer, assess the viability of the current problem-solving plan, and decide the best course of action: declare success, continue with the current plan, or pivot to a new one.

### The above includes:

* Question: The original software engineering task and its acceptance criteria. A correct implementation is believed to exist.
* Trajectory: The sequence of reasoning steps, tool calls, edits, and observations taken so far in the current attempt.
* Current Answer: The final answer produced by the current attempt.
* Budget Status: Current tool-call, token, and cost budget utilization and remaining budget.
* Test and Build Evidence: Commands, exit states, and observations that support or contradict completion.

### Your Task: A 3-Step Process

You must proceed in the following order:

#### Step 1: Conduct Verification Analysis

First, perform a strict verification of the Current Answer.
* Go through each constraint from the original Question one by one.
* For each constraint, compare it against the Current Answer, Trajectory, and Test and Build Evidence.
* State your finding for each constraint: satisfied, contradicted, or unverifiable.

#### Step 2: Make a Strategic Decision

Based on your verification and the budget, make one of three decisions.

1. SUCCESS: If the verification in Step 1 passed and all constraints are satisfied. The task is complete.
2. CONTINUE: If verification failed because only a few constraints are unverifiable, but the overall plan is still sound and salvageable. Choose this only if both are true:
   * Promising Path: The Trajectory is generally sound, and the failure was due to a correctable error.
   * Sufficient Budget: There is enough Remaining Budget to attempt a correction on this path.
3. PIVOT: If verification failed and the current plan should be abandoned for another one. Pivot when any are true:
   * Dead End: The Trajectory reveals a fundamental flaw that cannot be easily fixed.
   * Failed Tool Calls: The Trajectory shows repeated unsuccessful attempts or contradictory repository evidence.
   * Insufficient Budget: Remaining Budget is too low to make another meaningful correction within the current plan.

#### Step 3: Summarize for the Next Step

First provide a trajectory summary: concisely explain the initial goal, logical steps, key findings, test/build evidence, final conclusion, and any contradiction that changed the strategy.

For CONTINUE or PIVOT, details must contain:
* Failure Analysis: the root cause and general failure pattern to avoid.
* Useful Information: intermediate findings worth carrying forward.
* Strategic Recommendations: actionable next steps, including where to backtrack when useful.

### Output Requirement

Return one JSON object with exactly these fields and no surrounding text:
{
  "verification": "Constraint-by-constraint verification analysis",
  "decision": "SUCCESS | CONTINUE | PIVOT",
  "justification": "Concise strategic justification",
  "trajectory_summary": "Informative trajectory summary",
  "details": {}
}`;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredText(value: unknown, label: string): string {
	if (typeof value !== "string" || !value.trim()) {
		throw new Error(`${label} must be a nonempty string`);
	}
	return value.trim();
}

export function parseVerification(value: unknown): VerificationResult {
	const decoded: unknown = typeof value === "string" ? JSON.parse(value) : value;
	if (!isRecord(decoded) || ![...VERIFICATION_KEYS].every((key) => key in decoded)) {
		throw new Error("verification must be a JSON object with the five BATS fields");
	}
	if (Object.keys(decoded).length !== VERIFICATION_KEYS.size) {
		throw new Error("verification must contain exactly the five BATS fields");
	}
	const decision = decoded.decision;
	if (decision !== "SUCCESS" && decision !== "CONTINUE" && decision !== "PIVOT") {
		throw new Error("verification decision must be SUCCESS, CONTINUE, or PIVOT");
	}
	if (!isRecord(decoded.details)) {
		throw new Error("verification details must be a JSON object");
	}
	return {
		verification: requiredText(decoded.verification, "verification.verification"),
		decision,
		justification: requiredText(decoded.justification, "verification.justification"),
		trajectorySummary: requiredText(decoded.trajectory_summary, "verification.trajectory_summary"),
		details: decoded.details,
	};
}

export function parsePersistedVerification(value: unknown): PersistedVerification | undefined {
	if (
		!isRecord(value) ||
		typeof value.assistantEntryId !== "string" ||
		!value.assistantEntryId ||
		!Number.isSafeInteger(value.cycle) ||
		Number(value.cycle) < 1 ||
		(value.mode !== "interactive" && value.mode !== "batch") ||
		typeof value.task !== "string" ||
		!value.task ||
		typeof value.timestamp !== "number" ||
		!Number.isFinite(value.timestamp)
	) {
		return undefined;
	}
	try {
		const result = parseVerification({
			verification: value.verification,
			decision: value.decision,
			justification: value.justification,
			trajectory_summary: value.trajectorySummary,
			details: value.details,
		});
		return {
			...result,
			assistantEntryId: value.assistantEntryId,
			cycle: Number(value.cycle),
			mode: value.mode,
			task: value.task,
			timestamp: value.timestamp,
		};
	} catch {
		return undefined;
	}
}

function messageText(message: AgentMessage): string {
	if (message.role === "user") {
		return typeof message.content === "string"
			? message.content
			: message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n");
	}
	if (message.role === "assistant") {
		return message.content
			.flatMap((part) => {
				if (part.type === "text") return [part.text];
				if (part.type === "thinking") return [`Thinking: ${part.thinking}`];
				if (part.type === "toolCall") {
					return [`Tool call ${part.name}: ${JSON.stringify(part.arguments)}`];
				}
				return [];
			})
			.join("\n");
	}
	if (message.role === "toolResult") {
		return message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n");
	}
	if ("content" in message) {
		return typeof message.content === "string"
			? message.content
			: message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n");
	}
	return "";
}

function roleLabel(message: AgentMessage): string {
	if (message.role === "toolResult") return `Tool result (${message.toolName})`;
	return message.role.charAt(0).toUpperCase() + message.role.slice(1);
}

export function assistantText(message: AssistantMessage): string {
	return message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n").trim();
}

export function finalAssistant(messages: readonly AgentMessage[]): AssistantMessage | undefined {
	for (let index = messages.length - 1; index >= 0; index--) {
		const message = messages[index];
		if (message.role === "assistant") return message;
	}
	return undefined;
}

export function condenseTrajectory(branch: readonly SessionEntry[]): string {
	const sections = branch.flatMap((entry) => {
		if (entry.type === "message") {
			const text = messageText(entry.message).trim();
			return text ? [`${roleLabel(entry.message)}:\n${text}`] : [];
		}
		if (entry.type === "custom_message" && entry.customType === "synth.verifier.guidance") {
			const text = typeof entry.content === "string"
				? entry.content
				: entry.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n");
			return text.trim() ? [`Verifier guidance:\n${text.trim()}`] : [];
		}
		if (entry.type === "compaction") return [`Compaction summary:\n${entry.summary}`];
		return [];
	});
	const rendered = sections.join("\n\n");
	return rendered.length <= MAX_TRAJECTORY_CHARS ? rendered : rendered.slice(-MAX_TRAJECTORY_CHARS);
}

export function extractTestBuildEvidence(trajectory: string): string {
	const pattern = /(?:npm run|npx |pytest|ruff|mypy|vitest|build|test|PASS|FAIL|error|exit code|exit_code)/i;
	const evidence = trajectory
		.split("\n")
		.filter((line) => pattern.test(line))
		.join("\n");
	if (!evidence) return "No explicit test or build evidence was observed.";
	return evidence.length <= MAX_EVIDENCE_CHARS ? evidence : evidence.slice(-MAX_EVIDENCE_CHARS);
}

export function buildVerificationPrompt(
	task: string,
	trajectory: string,
	answer: string,
	budget: BudgetSnapshot,
): string {
	return `${BATS_CODING_VERIFIER_INSTRUCTION}

<question>
${task.trim()}
</question>

<trajectory>
${trajectory}
</trajectory>

<current_answer>
${answer}
</current_answer>

<budget_status>
${formatBudgetBlock(budget).trim()}
</budget_status>

<test_and_build_evidence>
${extractTestBuildEvidence(trajectory)}
</test_and_build_evidence>`;
}

function detailText(details: Record<string, unknown>, candidates: readonly string[]): string {
	for (const candidate of candidates) {
		const value = details[candidate];
		if (typeof value === "string" && value.trim()) return value.trim();
	}
	return "Not separately provided by the verifier.";
}

export function formatVerifierGuidance(result: VerificationResult, budget: BudgetSnapshot): string {
	const failureAnalysis = detailText(result.details, ["failure_analysis", "failureAnalysis", "Failure Analysis"]);
	const usefulInformation = detailText(result.details, [
		"useful_information",
		"usefulInformation",
		"Useful information",
	]);
	const recommendations = detailText(result.details, [
		"strategic_recommendations",
		"strategicRecommendations",
		"Strategic Recommendations",
	]);
	return `## Strategic verifier: ${result.decision}

Justification: ${result.justification}

Trajectory summary: ${result.trajectorySummary}

Failure Analysis: ${failureAnalysis}

Useful Information: ${usefulInformation}

Strategic Recommendations: ${recommendations}
${formatBudgetBlock(budget)}`;
}

export function extractLastPlan(text: string): string | undefined {
	const matches = [...text.matchAll(/<plan>[\s\S]*?<\/plan>/gi)];
	return matches.at(-1)?.[0];
}

export function buildCompactionPrompt(
	task: string,
	conversation: string,
	previousSummary: string | undefined,
	verifierSummary: string | undefined,
	activePlan: string | undefined,
): string {
	return `Summarize this software-engineering trajectory for continuation. Preserve goals, repository findings, edits, failed approaches, test/build evidence, unresolved risks, and the verifier's latest strategic direction. Do not claim unobserved success.

Task:
${task}

Previous summary:
${previousSummary ?? "None"}

Latest verifier summary:
${verifierSummary ?? "None"}

Active plan to preserve verbatim in the final summary:
${activePlan ?? "None"}

Conversation:
${conversation.slice(-MAX_TRAJECTORY_CHARS)}`;
}

export function appendActivePlan(summary: string, activePlan: string | undefined): string {
	const normalized = summary.trim();
	if (!activePlan || normalized.includes(activePlan)) return normalized;
	return `${normalized}\n\n## Active Plan\n\n${activePlan}`;
}

interface VerifierFixture {
	verifications: VerificationResult[];
	compaction?: string;
}

function loadFixture(path: string): VerifierFixture {
	let value: unknown;
	try {
		value = JSON.parse(readFileSync(path, "utf8"));
	} catch (error) {
		throw new Error(`cannot read verifier fixture ${path}: ${error instanceof Error ? error.message : String(error)}`);
	}
	if (!isRecord(value) || Object.keys(value).some((key) => !FIXTURE_KEYS.has(key))) {
		throw new Error("verifier fixture must contain only verifications and optional compaction");
	}
	if (!Array.isArray(value.verifications) || value.verifications.length === 0) {
		throw new Error("verifier fixture verifications must be a nonempty array");
	}
	if (value.compaction !== undefined && (typeof value.compaction !== "string" || !value.compaction.trim())) {
		throw new Error("verifier fixture compaction must be a nonempty string");
	}
	return {
		verifications: value.verifications.map(parseVerification),
		...(typeof value.compaction === "string" ? { compaction: value.compaction.trim() } : {}),
	};
}

function completionUrl(baseUrl: string): string {
	const url = new URL(baseUrl);
	if (
		(url.protocol !== "http:" && url.protocol !== "https:") ||
		url.username ||
		url.password ||
		url.search ||
		url.hash
	) {
		throw new Error("endpoint.baseUrl must be an HTTP(S) URL without credentials, query, or fragment");
	}
	return `${baseUrl.replace(/\/$/, "")}/chat/completions`;
}

function responseContent(value: unknown): string {
	if (!isRecord(value) || !Array.isArray(value.choices) || value.choices.length === 0) {
		throw new Error("verifier endpoint response has no choices");
	}
	const first = value.choices[0];
	if (!isRecord(first) || !isRecord(first.message)) {
		throw new Error("verifier endpoint returned an invalid choice");
	}
	return requiredText(first.message.content, "verifier endpoint content");
}

export class VerifierClient {
	private readonly fixture?: VerifierFixture;
	private fixtureIndex = 0;

	constructor(private readonly endpoint: SynthConfig["endpoint"]) {
		const fixturePath = process.env.SYNTH_VERIFIER_FIXTURE;
		if (fixturePath) this.fixture = loadFixture(fixturePath);
	}

	async verify(prompt: string): Promise<VerificationResult> {
		if (this.fixture) {
			const result = this.fixture.verifications[this.fixtureIndex++];
			if (!result) throw new Error("verifier fixture response sequence is exhausted");
			return result;
		}
		return parseVerification(await this.complete(prompt));
	}

	async summarize(prompt: string): Promise<string> {
		if (this.fixture) {
			if (!this.fixture.compaction) throw new Error("verifier fixture has no compaction response");
			return this.fixture.compaction;
		}
		return this.complete(prompt);
	}

	private async complete(prompt: string): Promise<string> {
		const endpoint = this.endpoint;
		if (!endpoint) throw new Error("endpoint is required for strategic verification");
		const headers: Record<string, string> = { "Content-Type": "application/json" };
		const apiKey = process.env[endpoint.apiKeyEnv];
		if (apiKey) headers.Authorization = `Bearer ${apiKey}`;
		const signal = AbortSignal.timeout(MODEL_TIMEOUT_MS);
		let response: Response;
		try {
			response = await fetch(completionUrl(endpoint.baseUrl), {
				method: "POST",
				headers,
				body: JSON.stringify({
					model: endpoint.smallModel,
					messages: [{ role: "user", content: prompt }],
					temperature: 0,
					stream: false,
				}),
				signal,
			});
		} catch (error) {
			throw new Error(`strategic verifier endpoint is unavailable: ${error instanceof Error ? error.message : String(error)}`);
		}
		if (!response.ok) throw new Error(`strategic verifier endpoint returned HTTP ${response.status}`);
		let value: unknown;
		try {
			value = await response.json();
		} catch (error) {
			throw new Error(`strategic verifier endpoint returned invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
		}
		return responseContent(value);
	}
}
