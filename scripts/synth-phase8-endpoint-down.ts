import assert from "node:assert/strict";
import { createServer } from "node:net";
import { join } from "node:path";
import { readFileSync, writeFileSync } from "node:fs";
import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import synthExtension from "../.prime/agent/extensions/synth/index.js";
import { emitSessionShutdownEvent } from "../packages/coding-agent/src/core/extensions/runner.js";
import {
	createHarness,
	getAssistantTexts,
} from "../packages/coding-agent/test/suite/harness.js";

async function closedLocalPort(): Promise<number> {
	const server = createServer();
	await new Promise<void>((resolveListen, rejectListen) => {
		server.once("error", rejectListen);
		server.listen(0, "127.0.0.1", resolveListen);
	});
	const address = server.address();
	if (!address || typeof address === "string") {
		server.close();
		throw new Error("failed to reserve a local endpoint port");
	}
	await new Promise<void>((resolveClose, rejectClose) => {
		server.close((error) => (error ? rejectClose(error) : resolveClose()));
	});
	return address.port;
}

const port = await closedLocalPort();
const harness = await createHarness({
	extensionFactories: [synthExtension],
	persistSession: true,
});
let shutdown = false;

try {
	writeFileSync(
		join(harness.tempDir, "synth.json"),
		`${JSON.stringify(
			{
				endpoint: {
					baseUrl: `http://127.0.0.1:${port}/v1`,
					model: "local-main",
					smallModel: "local-small",
					apiKeyEnv: "SYNTH_LOCAL_API_KEY",
				},
				telemetry: { dir: ".synth/telemetry" },
				budgets: {
					bash: 2,
					edit: 2,
					ipython: 2,
					subagents: 1,
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
	harness.setResponses([
		fauxAssistantMessage("The agent conclusion remains available when verification is offline."),
	]);

	await harness.session.prompt("Complete the endpoint-loss failure-injection task");
	await harness.session.waitForHeadlessIdle();

	assert.deepEqual(getAssistantTexts(harness), [
		"The agent conclusion remains available when verification is offline.",
	]);
	assert.equal(harness.getPendingResponseCount(), 0);
	const sessionFile = harness.sessionManager.getSessionFile();
	assert.ok(sessionFile);
	const entries = readFileSync(sessionFile, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as Record<string, unknown>);
	assert.equal(
		entries.some(
			(entry) =>
				entry.type === "custom" && entry.customType === "synth.verifier.decision",
		),
		false,
	);
	const telemetryPath = join(
		harness.tempDir,
		".synth",
		"telemetry",
		`${harness.sessionManager.getSessionId()}.jsonl`,
	);
	const failures = readFileSync(telemetryPath, "utf8")
		.trim()
		.split("\n")
		.map((line) => JSON.parse(line) as Record<string, unknown>)
		.filter(
			(event) =>
				event.errorClass === "VerifierUnavailable" &&
				(event.data as Record<string, unknown> | undefined)?.action ===
					"verifier.failed",
		);
	assert.equal(failures.length, 1);

	await emitSessionShutdownEvent(harness.session.extensionRunner, {
		type: "session_shutdown",
		reason: "reload",
	});
	shutdown = true;
	console.log(
		"synth-demo: Phase 8 endpoint-down PASS agent=completed verifier=degraded warnings=1",
	);
} finally {
	if (!shutdown) {
		await emitSessionShutdownEvent(harness.session.extensionRunner, {
			type: "session_shutdown",
			reason: "reload",
		}).catch(() => undefined);
	}
	harness.cleanup();
}
