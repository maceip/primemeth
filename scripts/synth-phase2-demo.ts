import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
	copyFileSync,
	mkdirSync,
	readFileSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { AgentTool } from "@earendil-works/pi-agent-core";
import { fauxAssistantMessage, fauxToolCall } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { Value } from "typebox/value";
import synthExtension from "../.prime/agent/extensions/synth/index.js";
import {
	TelemetryEventSchema,
	TelemetryWriter,
	type TelemetryEvent,
} from "../.prime/agent/extensions/synth/telemetry.js";
import { emitSessionShutdownEvent } from "../packages/coding-agent/src/core/extensions/runner.js";
import {
	createHarness,
	getAssistantTexts,
} from "../packages/coding-agent/test/suite/harness.js";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const telemetryOutput = process.env.SYNTH_PHASE2_TELEMETRY_OUT;
if (!telemetryOutput) {
	throw new Error("SYNTH_PHASE2_TELEMETRY_OUT is required");
}

function customMessages(messages: readonly { role: string }[]): string[] {
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

async function waitFor(predicate: () => boolean, label: string): Promise<void> {
	for (let attempt = 0; attempt < 100; attempt++) {
		if (predicate()) return;
		await new Promise<void>((resolveDelay) => setTimeout(resolveDelay, 20));
	}
	throw new Error(`timed out waiting for ${label}`);
}

let echoExecutions = 0;
let editExecutions = 0;
const echoTool: AgentTool = {
	name: "echo",
	label: "Echo",
	description: "Return a deterministic local value",
	parameters: Type.Object({ text: Type.String() }),
	execute: async (_toolCallId, params) => {
		echoExecutions++;
		const text =
			typeof params === "object" && params !== null && "text" in params
				? String(params.text)
				: "";
		return {
			content: [{ type: "text", text }],
			details: { echoed: true },
		};
	},
};
const editTool: AgentTool = {
	name: "edit",
	label: "Edit",
	description: "A demo fixture that must never reach execution for control-plane paths",
	parameters: Type.Object({
		path: Type.String(),
		oldText: Type.String(),
		newText: Type.String(),
	}),
	execute: async () => {
		editExecutions++;
		return {
			content: [{ type: "text", text: "unexpected edit execution" }],
			details: {},
		};
	},
};

const harness = await createHarness({
	extensionFactories: [synthExtension],
	persistSession: true,
	tools: [echoTool, editTool],
});
let shutdownEmitted = false;
const originalConsoleError = console.error;
const capturedWarnings: string[] = [];
console.error = (...values: unknown[]) => {
	const line = values.map(String).join(" ");
	if (line.startsWith("synth:")) capturedWarnings.push(line);
	originalConsoleError(...values);
};

try {
	writeFileSync(
		join(harness.tempDir, "synth.json"),
		readFileSync(join(repositoryRoot, "synth.json")),
	);
	writeFileSync(
		join(harness.tempDir, "pyproject.toml"),
		readFileSync(join(repositoryRoot, "pyproject.toml")),
	);
	symlinkSync(join(repositoryRoot, ".venv"), join(harness.tempDir, ".venv"), "dir");

	await harness.session.bindExtensions({});
	await harness.session.prompt("/synth status");
	const firstStatus = customMessages(harness.session.messages).at(-1);
	assert.match(firstStatus ?? "", /bridge=up pid=\d+ protocol=1 memory=pending/);
	assert.match(firstStatus ?? "", /configEcho=.*\"telemetry\":/);
	const bridgePid = Number(firstStatus?.match(/bridge=up pid=(\d+)/)?.[1]);
	assert.ok(Number.isSafeInteger(bridgePid) && bridgePid > 1);
	const bridgeCommand = execFileSync("/bin/ps", [
		"-p",
		String(bridgePid),
		"-o",
		"command=",
	], { encoding: "utf8" }).trim();
	assert.match(bridgeCommand, /synth-bridge/);
	console.log(
		`synth-demo: pre-kill process receipt bridge_pid=${bridgePid} command=${bridgeCommand}`,
	);
	process.kill(bridgePid, "SIGKILL");
	await waitFor(() => capturedWarnings.length === 1, "one bridge warning");

	await harness.session.prompt("/synth status");
	const secondStatus = customMessages(harness.session.messages).at(-1);
	assert.match(secondStatus ?? "", /bridge=disabled error=bridge exited unexpectedly/);
	await harness.session.prompt("/synth budget");
	const budgetStatus = customMessages(harness.session.messages).at(-1) ?? "";
	assert.match(budgetStatus, /Synth budget state/);
	assert.match(budgetStatus, /<budget>/);
	assert.match(budgetStatus, /Regime: HIGH/);

	harness.setResponses([
		fauxAssistantMessage(fauxToolCall("echo", { text: "phase-two" }), {
			stopReason: "toolUse",
		}),
		fauxAssistantMessage(
			fauxToolCall("edit", {
				path: ".prime/agent/extensions/synth/index.ts",
				oldText: "before",
				newText: "after",
			}),
			{ stopReason: "toolUse" },
		),
		fauxAssistantMessage("Phase 2 survived the bridge crash."),
	]);
	await harness.session.prompt("Exercise telemetry and the runtime write guard");
	assert.equal(echoExecutions, 1);
	assert.equal(editExecutions, 0);
	assert.equal(getAssistantTexts(harness).at(-1), "Phase 2 survived the bridge crash.");

	await emitSessionShutdownEvent(harness.session.extensionRunner, {
		type: "session_shutdown",
		reason: "quit",
	});
	shutdownEmitted = true;

	const sessionId = harness.sessionManager.getSessionId();
	const telemetryPath = join(
		harness.tempDir,
		".synth",
		"telemetry",
		`${sessionId}.jsonl`,
	);
	const lines = readFileSync(telemetryPath, "utf8").trim().split("\n");
	const events: TelemetryEvent[] = lines.map((line, index) => {
		const value: unknown = JSON.parse(line);
		assert.ok(Value.Check(TelemetryEventSchema, value), `invalid event ${index + 1}`);
		return value;
	});
	assert.deepEqual(
		events.map((event) => event.sequence),
		events.map((_, index) => index + 1),
	);
	const kinds = new Set(events.map((event) => event.kind));
	for (const kind of [
		"session.start",
		"session.shutdown",
		"session.outcome",
		"turn",
		"tool",
		"provider",
		"agent.end",
		"bridge",
	] as const) {
		assert.ok(kinds.has(kind), `telemetry omitted ${kind}`);
	}
	const outcomeEvents = events.filter((event) => event.kind === "session.outcome");
	assert.equal(outcomeEvents.length, 1);
	assert.equal(outcomeEvents[0]?.outcome?.reward, null);
	assert.equal(outcomeEvents[0]?.outcome?.termination_reason, "UNKNOWN");
	assert.equal(outcomeEvents[0]?.outcome?.evidence.kind, "transcript");
	assert.equal(outcomeEvents[0]?.outcome?.evidence.uri, harness.sessionManager.getSessionFile());
	assert.ok(
		events.some(
			(event) =>
				event.kind === "tool" &&
				event.tool?.name === "echo" &&
				!event.tool.isError &&
				event.transcript?.entryId,
		),
		"successful tool telemetry lacks a native transcript pointer",
	);
	assert.ok(
		events.some(
			(event) =>
				event.kind === "tool" &&
				event.tool?.name === "edit" &&
				event.errorClass === "ControlPlaneWriteBlocked",
		),
		"control-plane denial was not audited",
	);
	assert.ok(
		events.some(
			(event) =>
				event.kind === "provider" &&
				event.provider?.provider === "faux" &&
				event.usage !== undefined &&
				event.transcript?.entryId,
		),
		"provider telemetry lacks native usage or transcript identity",
	);
	assert.equal(capturedWarnings.length, 1);
	const sessionFile = harness.sessionManager.getSessionFile();
	assert.ok(sessionFile);
	const transcript = readFileSync(sessionFile, "utf8");
	for (const event of events) {
		if (event.transcript?.entryId) {
			assert.ok(
				transcript.includes(`\"id\":\"${event.transcript.entryId}\"`),
				`transcript entry is missing: ${event.transcript.entryId}`,
			);
		}
	}
	const corruptDir = join(harness.tempDir, ".synth", "corrupt-telemetry");
	mkdirSync(corruptDir, { recursive: true });
	writeFileSync(join(corruptDir, "corrupt-session.jsonl"), "not-json\n");
	assert.throws(
		() => new TelemetryWriter(corruptDir, "corrupt-session"),
		/cannot resume telemetry file/,
	);
	const resumedWriter = new TelemetryWriter(dirname(telemetryPath), sessionId);
	assert.equal(resumedWriter.hasTerminalOutcome(), true);
	assert.throws(
		() =>
			resumedWriter.append({
				sessionId,
				kind: "session.outcome",
				outcome: {
					reward: null,
					termination_reason: "UNKNOWN",
					evidence: { kind: "artifact", uri: telemetryPath, entry_id: null },
				},
			}),
		/already contains a terminal outcome/,
	);
	copyFileSync(telemetryPath, telemetryOutput);
	console.log(
		`synth-demo: Phase 2 extension spine PASS events=${events.length} warnings=${capturedWarnings.length}`,
	);
} finally {
	console.error = originalConsoleError;
	if (!shutdownEmitted) {
		await emitSessionShutdownEvent(harness.session.extensionRunner, {
			type: "session_shutdown",
			reason: "quit",
		}).catch(() => undefined);
	}
	harness.cleanup();
}
