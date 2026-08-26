import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { fauxAssistantMessage, type Context, type FauxResponseFactory } from "@earendil-works/pi-ai";
import synthExtension from "../.prime/agent/extensions/synth/index.js";
import { loadSynthConfig } from "../.prime/agent/extensions/synth/config.js";
import {
	appendActivePlan,
	extractLastPlan,
	parseVerification,
	VerifierClient,
} from "../.prime/agent/extensions/synth/verifier.js";
import { emitSessionShutdownEvent } from "../packages/coding-agent/src/core/extensions/runner.js";
import { createHarness } from "../packages/coding-agent/test/suite/harness.js";

const demoDir = mkdtempSync(join(tmpdir(), "synth-phase5-demo-"));
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixturePath = join(demoDir, "verifier-fixture.json");
const originalFixture = process.env.SYNTH_VERIFIER_FIXTURE;

function contentText(message: AgentMessage): string {
	if (message.role === "user") {
		return typeof message.content === "string"
			? message.content
			: message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n");
	}
	if (message.role === "assistant" || message.role === "toolResult") {
		return message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n");
	}
	if ("content" in message) {
		return typeof message.content === "string"
			? message.content
			: message.content.flatMap((part) => (part.type === "text" ? [part.text] : [])).join("\n");
	}
	return "";
}

function captureContext(target: Context[]): FauxResponseFactory {
	return (context) => {
		target.push({ ...context, messages: structuredClone(context.messages) });
		return fauxAssistantMessage("The corrected second attempt now satisfies the acceptance criteria.");
	};
}

function run(command: string, args: readonly string[], cwd: string): string {
	const result = spawnSync(command, args, { cwd, encoding: "utf8" });
	if (result.status !== 0) {
		throw new Error(
			`${command} ${args.join(" ")} failed (${result.status ?? "signal"}): ${result.stderr || result.stdout}`,
		);
	}
	return result.stdout;
}

writeFileSync(
	fixturePath,
	`${JSON.stringify(
		{
			verifications: [
				{
					verification: "The first answer contradicts the required corrected behavior.",
					decision: "PIVOT",
					justification: "The current approach is a dead end.",
					trajectory_summary: "Attempt one asserted success without the required correction.",
					details: {
						failure_analysis: "The first attempt used the wrong implementation strategy.",
						useful_information: "The acceptance criterion requires corrected behavior.",
						strategic_recommendations: "Abandon the first approach and implement the correction.",
					},
				},
				{
					verification: "The second answer satisfies the requested corrected behavior.",
					decision: "SUCCESS",
					justification: "All fixture acceptance criteria are satisfied.",
					trajectory_summary: "Verifier guidance pivoted attempt one into a corrected attempt two.",
					details: {},
				},
			],
			compaction: "The batch trajectory remains focused on the corrected implementation.",
		},
		null,
		2,
	)}\n`,
);
process.env.SYNTH_VERIFIER_FIXTURE = fixturePath;

const harness = await createHarness({ extensionFactories: [synthExtension], persistSession: true });
let shutdown = false;

try {
	writeFileSync(
		join(harness.tempDir, "synth.json"),
		`${JSON.stringify(
			{
				endpoint: {
					baseUrl: "http://localhost:8000/v1",
					model: "local-main",
					smallModel: "local-small",
					apiKeyEnv: "SYNTH_LOCAL_API_KEY",
				},
				telemetry: { dir: ".synth/telemetry" },
				budgets: {
					bash: 4,
					edit: 4,
					ipython: 4,
					subagents: 2,
					tokens: 0,
					costUsd: 0,
				},
				verifier: {
					interactive: true,
					batch: true,
					maxCycles: 2,
					contGen: { enabled: true, minBudgetFrac: 0.2 },
				},
			},
			null,
			2,
		)}\n`,
	);
	await harness.session.bindExtensions({});

	const contexts: Context[] = [];
	harness.setResponses([
		fauxAssistantMessage("The uncorrected first attempt is complete."),
		captureContext(contexts),
	]);
	await harness.session.prompt("Implement the corrected behavior and prove the acceptance criterion");
	await harness.session.waitForHeadlessIdle();

	const assistantMessages = harness.session.messages.filter((message) => message.role === "assistant");
	assert.deepEqual(assistantMessages.map(contentText), [
		"The uncorrected first attempt is complete.",
		"The corrected second attempt now satisfies the acceptance criteria.",
	]);
	assert.equal(harness.getPendingResponseCount(), 0);
	const correctedContext = contexts.at(-1)?.messages.map(contentText).join("\n") ?? "";
	assert.ok(
		correctedContext.includes("## Strategic verifier: PIVOT") &&
			correctedContext.includes("Failure Analysis:") &&
			correctedContext.includes("Strategic Recommendations:") &&
			correctedContext.includes("<budget>"),
		"attempt two did not receive the PIVOT guidance and budget state",
	);

	const sessionFile = harness.sessionManager.getSessionFile();
	assert.ok(sessionFile);
	const persisted = readFileSync(sessionFile, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as Record<string, unknown>)
		.filter((entry) => entry.type === "custom" && entry.customType === "synth.verifier.decision")
		.map((entry) => entry.data as Record<string, unknown>);
	assert.deepEqual(
		persisted.map((decision) => [decision.cycle, decision.decision]),
		[
			[1, "PIVOT"],
			[2, "SUCCESS"],
		],
	);

	const telemetryPath = join(
		harness.tempDir,
		".synth",
		"telemetry",
		`${harness.sessionManager.getSessionId()}.jsonl`,
	);
	const verifierTelemetry = readFileSync(telemetryPath, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as { data?: Record<string, unknown> })
		.filter((event) => event.data?.action === "verifier.decision");
	assert.deepEqual(verifierTelemetry.map((event) => event.data?.decision), ["PIVOT", "SUCCESS"]);

	assert.throws(
		() =>
			parseVerification({
				verification: "checked",
				decision: "SUCCESS",
				justification: "done",
				trajectory_summary: "summary",
				details: {},
				extra: true,
			}),
		/exactly/,
	);
	const plan = extractLastPlan("old <plan>[ ] first</plan> new <plan>[~] corrected</plan>");
	assert.equal(plan, "<plan>[~] corrected</plan>");
	assert.ok(appendActivePlan("Summary", plan).endsWith(`## Active Plan\n\n${plan}`));

	const originalFetch = globalThis.fetch;
	delete process.env.SYNTH_VERIFIER_FIXTURE;
	let responsesRequest: { url?: string; body?: Record<string, unknown> } = {};
	const liveFixtureJson = JSON.stringify({
		verification: "The live-protocol fixture satisfies the constraint.",
		decision: "SUCCESS",
		justification: "The Responses payload and parser are connected.",
		trajectory_summary: "The verifier returned structured output text.",
		details: {},
	});
	globalThis.fetch = async (input: string | URL | Request, init?: RequestInit) => {
		responsesRequest = {
			url: String(input),
			body: JSON.parse(String(init?.body)) as Record<string, unknown>,
		};
		if (String(input).endsWith("/chat/completions")) {
			return new Response(
				JSON.stringify({ choices: [{ message: { content: liveFixtureJson } }] }),
				{ status: 200, headers: { "Content-Type": "application/json" } },
			);
		}
		const splitAt = Math.floor(liveFixtureJson.length / 2);
		return new Response(
			JSON.stringify({
				status: "completed",
				output: [
					{
						type: "message",
						content: [
							{ type: "output_text", text: liveFixtureJson.slice(0, splitAt) },
							{ type: "output_text", text: liveFixtureJson.slice(splitAt) },
						],
					},
				],
			}),
			{ status: 200, headers: { "Content-Type": "application/json" } },
		);
	};
	try {
		const result = await new VerifierClient({
			baseUrl: "https://api.openai.com/v1",
			model: "gpt-5.4-mini",
			smallModel: "gpt-5.4-mini",
			apiKeyEnv: "SYNTH_TEST_UNUSED_API_KEY",
			api: "openai-responses",
		}).verify("Verify the Responses API path");
		assert.equal(result.decision, "SUCCESS");
		assert.equal(responsesRequest.url, "https://api.openai.com/v1/responses");
		assert.equal(responsesRequest.body?.input, "Verify the Responses API path");
		assert.equal(responsesRequest.body?.store, false);
		assert.ok(!Object.hasOwn(responsesRequest.body ?? {}, "messages"));

		const chatResult = await new VerifierClient({
			baseUrl: "http://localhost:8000/v1",
			model: "local-main",
			smallModel: "local-small",
			apiKeyEnv: "SYNTH_TEST_UNUSED_API_KEY",
			api: "openai-completions",
		}).verify("Verify the Chat Completions path");
		assert.equal(chatResult.decision, "SUCCESS");
		assert.equal(responsesRequest.url, "http://localhost:8000/v1/chat/completions");
		assert.deepEqual(responsesRequest.body?.messages, [
			{ role: "user", content: "Verify the Chat Completions path" },
		]);
		assert.equal(responsesRequest.body?.temperature, 0);
		assert.ok(!Object.hasOwn(responsesRequest.body ?? {}, "input"));

		globalThis.fetch = async () =>
			new Response(
				JSON.stringify({
					status: "incomplete",
					incomplete_details: { reason: "max_output_tokens" },
				}),
				{ status: 200, headers: { "Content-Type": "application/json" } },
			);
		await assert.rejects(
			new VerifierClient({
				baseUrl: "https://api.openai.com/v1",
				model: "gpt-5.4-mini",
				smallModel: "gpt-5.4-mini",
				apiKeyEnv: "SYNTH_TEST_UNUSED_API_KEY",
				api: "openai-responses",
			}).verify("Reject incomplete output"),
			/max_output_tokens/,
		);
	} finally {
		globalThis.fetch = originalFetch;
		process.env.SYNTH_VERIFIER_FIXTURE = fixturePath;
	}

	const configFixtureDir = join(demoDir, "config-fixture");
	mkdirSync(configFixtureDir);
	const configFixturePath = join(configFixtureDir, "synth.json");
	const endpointFixture = {
		baseUrl: "http://localhost:8000/v1",
		model: "local-main",
		smallModel: "local-small",
		apiKeyEnv: "SYNTH_LOCAL_API_KEY",
	};
	writeFileSync(configFixturePath, JSON.stringify({ endpoint: endpointFixture }));
	const defaultProtocol = loadSynthConfig(configFixtureDir);
	assert.equal(defaultProtocol.state, "loaded");
	assert.equal(defaultProtocol.config?.endpoint?.api, "openai-completions");
	writeFileSync(
		configFixturePath,
		JSON.stringify({ endpoint: { ...endpointFixture, api: "openai-responses" } }),
	);
	assert.equal(loadSynthConfig(configFixtureDir).config?.endpoint?.api, "openai-responses");
	writeFileSync(configFixturePath, JSON.stringify({ endpoint: { ...endpointFixture, api: "bad" } }));
	assert.equal(loadSynthConfig(configFixtureDir).state, "invalid");

	const targetRepo = join(demoDir, "batch-target");
	mkdirSync(targetRepo);
	run("git", ["init", "-q"], targetRepo);
	run(
		"git",
		[
			"-c",
			"user.name=Synth Fixture",
			"-c",
			"user.email=synth@example.invalid",
			"commit",
			"--allow-empty",
			"-q",
			"-m",
			"base",
		],
		targetRepo,
	);
	const batchOutput = run(
		"uv",
		[
			"run",
			"--project",
			repositoryRoot,
			"--no-sync",
			"synth",
			"run",
			"--task",
			"Implement the corrected batch fixture",
			"--bats",
			"--target",
			targetRepo,
			"--max-trajectories",
			"1",
			"--run-id",
			"phase5-demo",
			"--fixture-cli",
			join(repositoryRoot, "tests", "batch_fixtures", "fake_coding_agent.py"),
			"--judge-fixture",
			join(repositoryRoot, "tests", "batch_fixtures", "judge_one.json"),
		],
		repositoryRoot,
	);
	const batchResult = JSON.parse(batchOutput) as {
		candidatesRecorded?: number;
		selection?: string;
		metrics?: Record<string, number>;
	};
	assert.equal(batchResult.candidatesRecorded, 1);
	assert.deepEqual(batchResult.metrics, { first: 1, pass: 1, select: 1 });
	assert.ok(batchResult.selection && existsSync(batchResult.selection));
	const runDir = join(targetRepo, ".synth", "runs", "phase5-demo");
	const batchSession = readFileSync(
		join(runDir, "trajectories", "1", "session", "session-1.jsonl"),
		"utf8",
	);
	assert.ok(
		batchSession.includes('"decision": "PIVOT"') &&
			batchSession.includes("synth.verifier.guidance") &&
			batchSession.includes('"decision": "SUCCESS"') &&
			existsSync(join(runDir, "candidates", "1", "diff.patch")) &&
			existsSync(join(runDir, "selection.json")),
		"product batch artifacts omitted the pivot, corrected candidate, or selection",
	);

	await emitSessionShutdownEvent(harness.session.extensionRunner, {
		type: "session_shutdown",
		reason: "reload",
	});
	shutdown = true;
	console.log(
		"synth-demo: Phase 5 BATS PASS attempt1=PIVOT attempt2=SUCCESS candidates=1 selection=1",
	);
} finally {
	if (!shutdown) {
		await emitSessionShutdownEvent(harness.session.extensionRunner, {
			type: "session_shutdown",
			reason: "reload",
		}).catch(() => undefined);
	}
	harness.cleanup();
	if (originalFixture === undefined) delete process.env.SYNTH_VERIFIER_FIXTURE;
	else process.env.SYNTH_VERIFIER_FIXTURE = originalFixture;
	rmSync(demoDir, { recursive: true, force: true });
}
