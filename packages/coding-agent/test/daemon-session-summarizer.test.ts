import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { fauxAssistantMessage } from "@earendil-works/pi-ai";
import { describe, expect, test } from "vitest";
import type { AgentStatus } from "../src/core/session-manager.js";
import {
	AGENT_STATUS_SYSTEM_PROMPT,
	agentStatusChanged,
	buildStatusContext,
	generateAgentStatus,
	parseAgentStatusResponse,
} from "../src/modes/daemon/daemon-session-summarizer.js";
import { createHarness } from "./suite/harness.js";

function userMessage(text: string): AgentMessage {
	return { role: "user", content: [{ type: "text", text }], timestamp: 0 } as unknown as AgentMessage;
}

function assistantMessage(text: string, tools: string[] = []): AgentMessage {
	const content = [{ type: "text", text }, ...tools.map((name) => ({ type: "tool_use", name, id: name, input: {} }))];
	return { role: "assistant", content, timestamp: 0 } as unknown as AgentMessage;
}

describe("daemon session summarizer", () => {
	describe("generateAgentStatus", () => {
		test("uses the configured local model with bounded provider options", async () => {
			const harness = await createHarness({
				provider: "local",
				models: [
					{ id: "local-main", reasoning: false },
					{ id: "local-small", reasoning: false },
				],
				settings: { daemonSessionSummary: { provider: "local", model: "local-small" } },
			});
			try {
				harness.setResponses([
					(context, options, _state, model) => {
						expect(model).toMatchObject({ provider: "local", id: "local-small" });
						expect(context.systemPrompt).toBe(AGENT_STATUS_SYSTEM_PROMPT);
						expect(context.messages[0]?.content).toEqual([
							{
								type: "text",
								text: "<agent-state>idle (finished its turn)</agent-state>\n<conversation>\nuser: finish the demo\n</conversation>",
							},
						]);
						expect(options).toMatchObject({ maxTokens: 400, timeoutMs: 5_000, maxRetries: 0 });
						return fauxAssistantMessage(
							"<recap>Completed the local recap demo</recap>\n<status>COMPLETED</status>",
						);
					},
				]);

				await expect(
					generateAgentStatus({
						registry: harness.session.modelRegistry,
						settingsManager: harness.settingsManager,
						messages: [userMessage("finish the demo")],
						isWorking: false,
					}),
				).resolves.toEqual({ summary: "Completed the local recap demo", taskState: "completed" });
				expect(harness.faux.state.callCount).toBe(1);
			} finally {
				harness.cleanup();
			}
		});

		test("does not call a provider when summary selection is absent, partial, or unavailable", async () => {
			const settingsCases = [
				undefined,
				{ daemonSessionSummary: { provider: "local" } },
				{ daemonSessionSummary: { provider: "local", model: "missing" } },
			];
			for (const settings of settingsCases) {
				const harness = await createHarness({
					provider: "local",
					models: [{ id: "local-small", reasoning: false }],
					settings,
				});
				try {
					await expect(
						generateAgentStatus({
							registry: harness.session.modelRegistry,
							settingsManager: harness.settingsManager,
							messages: [userMessage("finish the demo")],
							isWorking: false,
						}),
					).resolves.toBeUndefined();
					expect(harness.faux.state.callCount).toBe(0);
				} finally {
					harness.cleanup();
				}
			}
		});

		test("returns undefined for aborted and unparseable provider responses", async () => {
			const harness = await createHarness({
				provider: "local",
				models: [{ id: "local-small", reasoning: false }],
				settings: { daemonSessionSummary: { provider: "local", model: "local-small" } },
			});
			try {
				harness.setResponses([
					fauxAssistantMessage("partial recap", { stopReason: "aborted" }),
					fauxAssistantMessage("Completed without the required tags"),
				]);
				for (let attempt = 0; attempt < 2; attempt++) {
					await expect(
						generateAgentStatus({
							registry: harness.session.modelRegistry,
							settingsManager: harness.settingsManager,
							messages: [userMessage("finish the demo")],
							isWorking: false,
						}),
					).resolves.toBeUndefined();
				}
				expect(harness.faux.state.callCount).toBe(2);
			} finally {
				harness.cleanup();
			}
		});
	});

	describe("parseAgentStatusResponse", () => {
		test("parses recap and completion verdict for an idle session", () => {
			const result = parseAgentStatusResponse(
				"<recap>Added the API reference page</recap>\n<status>COMPLETED</status>",
				false,
			);
			expect(result).toEqual({ summary: "Added the API reference page", taskState: "completed" });
		});

		test("maps NEEDS_INPUT for idle sessions", () => {
			const result = parseAgentStatusResponse(
				"<recap>Asked which database to target</recap>\n<status>NEEDS_INPUT</status>",
				false,
			);
			expect(result?.taskState).toBe("needs_input");
		});

		test("omits the verdict while working and ignores any status tag", () => {
			const result = parseAgentStatusResponse(
				"<recap>Refactoring token validation</recap>\n<status>COMPLETED</status>",
				true,
			);
			expect(result).toEqual({ summary: "Refactoring token validation" });
		});

		test("falls back to needs_input on a missing or unrecognized idle verdict", () => {
			expect(
				parseAgentStatusResponse("<recap>Doing something</recap>\n<status>MAYBE</status>", false)?.taskState,
			).toBe("needs_input");
			expect(parseAgentStatusResponse("<recap>Doing something</recap>", false)?.taskState).toBe("needs_input");
		});

		test("ignores narration outside the tags and never leaks free-form text", () => {
			// A chatty/reasoning model that narrates instead of using tags yields no recap.
			expect(parseAgentStatusResponse("Investigating the failing test.", true)).toBeUndefined();
			expect(
				parseAgentStatusResponse(
					"Recap: . So: <recap>Curating a niche list of Muon optimizer papers</recap>",
					true,
				),
			).toEqual({ summary: "Curating a niche list of Muon optimizer papers" });
		});

		test("ignores reasoning prose around the tags", () => {
			const text =
				"Let me decide. The agent finished editing.\n<recap>Updated the login handler</recap>\n<status>COMPLETED</status>";
			expect(parseAgentStatusResponse(text, false)).toEqual({
				summary: "Updated the login handler",
				taskState: "completed",
			});
		});

		test("rejects an echoed prompt template", () => {
			const echoed =
				"<recap>a present-tense clause, at most 12 words, no trailing period</recap>\n<status>COMPLETED</status>";
			expect(parseAgentStatusResponse(echoed, true)).toBeUndefined();
		});

		test("returns undefined when no recap tag is present", () => {
			expect(parseAgentStatusResponse("", false)).toBeUndefined();
			expect(parseAgentStatusResponse("<status>COMPLETED</status>", false)).toBeUndefined();
		});

		test("drops chain-of-thought that falls outside the closing recap tag", () => {
			const text =
				"<recap>Sending SSH auth retry to tcg-autoresearch-rl</recap> That's 5 words? Count: Sending(1) SSH(2) = 6 words.\n<status>NEEDS_INPUT</status>";
			expect(parseAgentStatusResponse(text, true)).toEqual({
				summary: "Sending SSH auth retry to tcg-autoresearch-rl",
			});
		});

		test("rejects a recap body that is nothing but counting artifacts", () => {
			expect(parseAgentStatusResponse("<recap>(1) word(2) count(3) = 3 words</recap>", true)).toBeUndefined();
		});

		test("rejects a rambling recap that blows past the word ceiling", () => {
			const text =
				"<recap>this is a very long rambling sentence that just keeps going and going well past any reasonable recap length</recap>";
			expect(parseAgentStatusResponse(text, true)).toBeUndefined();
		});

		test("strips wrapping quotes the model adds around the recap", () => {
			const text = '<recap>"Wiring the recap line"</recap>\n<status>COMPLETED</status>';
			expect(parseAgentStatusResponse(text, false)).toEqual({
				summary: "Wiring the recap line",
				taskState: "completed",
			});
		});

		test("ignores an open recap tag with no close", () => {
			expect(
				parseAgentStatusResponse("<recap>Editing the parser\n<status>NEEDS_INPUT</status>", true),
			).toBeUndefined();
		});

		test("takes the last recap tag when a draft is corrected", () => {
			const text = "<recap>Draft recap</recap>\n<recap>Final corrected recap</recap>";
			expect(parseAgentStatusResponse(text, true)).toEqual({ summary: "Final corrected recap" });
		});

		test("takes the last status tag when a draft is corrected", () => {
			const text = "<recap>Editing the parser</recap>\n<status>NEEDS_INPUT</status>\n<status>COMPLETED</status>";
			expect(parseAgentStatusResponse(text, false)?.taskState).toBe("completed");
		});

		test("normalizes unicode angle-bracket lookalikes around the tags", () => {
			// The model sometimes emits › ‹ instead of > < ; normalize so the tag still parses.
			const text = "‹recap›Curating a niche list of Muon optimizer papers‹/recap›";
			expect(parseAgentStatusResponse(text, true)).toEqual({
				summary: "Curating a niche list of Muon optimizer papers",
			});
		});
	});

	describe("buildStatusContext", () => {
		test("includes the agent state and the trailing conversation with tool names", () => {
			const context = buildStatusContext(
				[userMessage("add a login endpoint"), assistantMessage("Editing the router", ["Edit", "Bash"])],
				true,
			);
			expect(context).toContain("<agent-state>working</agent-state>");
			expect(context).toContain("user: add a login endpoint");
			expect(context).toContain("assistant: Editing the router [tools: Edit, Bash]");
		});

		test("marks idle sessions as finished", () => {
			expect(buildStatusContext([userMessage("hi")], false)).toContain("idle (finished its turn)");
		});

		test("only keeps the most recent messages", () => {
			const messages = Array.from({ length: 20 }, (_, i) => userMessage(`message ${i}`));
			const context = buildStatusContext(messages, false);
			expect(context).toContain("message 19");
			expect(context).not.toContain("message 0\n");
		});
	});

	describe("agentStatusChanged", () => {
		const base: AgentStatus = { summary: "Working on it", taskState: "needs_input", basedOnMessageCount: 4 };
		test("is true when there is no previous status", () => {
			expect(agentStatusChanged(undefined, { summary: "x" })).toBe(true);
		});
		test("detects summary and verdict changes", () => {
			expect(agentStatusChanged(base, { summary: "Working on it", taskState: "needs_input" })).toBe(false);
			expect(agentStatusChanged(base, { summary: "Done", taskState: "needs_input" })).toBe(true);
			expect(agentStatusChanged(base, { summary: "Working on it", taskState: "completed" })).toBe(true);
		});
	});
});
