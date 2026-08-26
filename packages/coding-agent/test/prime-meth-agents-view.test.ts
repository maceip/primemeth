import { describe, expect, it } from "vitest";
import {
	type AgentsViewPersistentState,
	claimAgentsViewPrimeMethBoot,
	createInitialAgentsViewPersistentState,
} from "../src/modes/agents-view/agents-view-mode.js";

describe("Prime METH agents-view boot ownership", () => {
	it("animates only the first view in a persistent agents-view run", () => {
		const state: AgentsViewPersistentState = {};

		expect(claimAgentsViewPrimeMethBoot(state)).toBeUndefined();
		expect(claimAgentsViewPrimeMethBoot(state)).toBe(false);
	});

	it("starts settled when navigation comes from an already-rendered chat", () => {
		const state = createInitialAgentsViewPersistentState({
			suppressPrimeMethBootAnimation: true,
		});

		expect(claimAgentsViewPrimeMethBoot(state)).toBe(false);
	});
});
