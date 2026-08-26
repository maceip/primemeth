import { visibleWidth } from "@earendil-works/pi-tui";
import stripAnsi from "strip-ansi";
import { beforeAll, describe, expect, it } from "vitest";
import type { ReadonlyFooterDataProvider } from "../src/core/footer-data-provider.js";
import { FooterComponent } from "../src/modes/interactive/components/footer.js";
import { initTheme } from "../src/modes/interactive/theme/theme.js";

function createFooterData(
	providerCount: number,
	statuses: ReadonlyMap<string, string> = new Map(),
): ReadonlyFooterDataProvider {
	const provider = {
		getGitBranch: () => "main",
		getExtensionStatuses: () => statuses,
		getAvailableProviderCount: () => providerCount,
		onBranchChange: (callback: () => void) => {
			void callback;
			return () => {};
		},
	};

	return provider;
}

describe("FooterComponent width handling", () => {
	beforeAll(() => {
		initTheme(undefined, false);
	});

	it("keeps all lines within width for narrow provider data", () => {
		const width = 93;
		const footer = new FooterComponent(createFooterData(1));

		const lines = footer.render(width);
		for (const line of lines) {
			expect(visibleWidth(line)).toBeLessThanOrEqual(width);
		}
	});

	it("keeps all lines within width for wide provider data", () => {
		const width = 60;
		const footer = new FooterComponent(createFooterData(2));

		const lines = footer.render(width);
		for (const line of lines) {
			expect(visibleWidth(line)).toBeLessThanOrEqual(width);
		}
	});

	it("renders live model, cwd, and throughput on one anchored line", () => {
		let model = "local-main";
		let cwd = "/tmp/synth";
		const footer = new FooterComponent(
			createFooterData(1, new Map([["synth.performance", "prefill 123 tok/s  ·  decode 45.6 tok/s"]])),
			() => model,
			() => cwd,
		);

		const first = footer.render(120);
		expect(first).toHaveLength(1);
		expect(stripAnsi(first[0]!)).toBe(
			"model local-main  ·  cwd /tmp/synth  ·  prefill 123 tok/s  ·  decode 45.6 tok/s",
		);

		model = "local-small";
		cwd = "/tmp/next";
		expect(stripAnsi(footer.render(120)[0]!)).toContain("model local-small  ·  cwd /tmp/next");
	});

	it("degrades to live model and cwd before any extension status exists", () => {
		const footer = new FooterComponent(
			createFooterData(1),
			() => "local-main",
			() => "/tmp/synth",
		);

		expect(stripAnsi(footer.render(80)[0]!)).toBe("model local-main  ·  cwd /tmp/synth");
	});

	it("sanitizes extension status text and orders status keys", () => {
		const footer = new FooterComponent(
			createFooterData(
				1,
				new Map([
					["zeta", "last\tvalue"],
					["alpha", "first\nvalue"],
				]),
			),
			() => "model\nname",
			() => "/tmp\tpath",
		);
		const output = stripAnsi(footer.render(120)[0]!);

		expect(output).toContain("model model name");
		expect(output).toContain("cwd /tmp path");
		expect(output.indexOf("first value")).toBeLessThan(output.indexOf("last value"));
		expect(output).not.toMatch(/[\r\n\t]/);
	});

	it("truncates a long cwd before dropping last-turn throughput", () => {
		const width = 90;
		const footer = new FooterComponent(
			createFooterData(1, new Map([["synth.performance", "prefill 123 tok/s  ·  decode 45 tok/s"]])),
			() => "local-main",
			() => `/tmp/${"nested/".repeat(20)}synth`,
		);
		const output = stripAnsi(footer.render(width)[0]!);

		expect(visibleWidth(footer.render(width)[0]!)).toBeLessThanOrEqual(width);
		expect(output).toContain("prefill 123 tok/s");
		expect(output).toContain("decode 45 tok/s");
		expect(output).toMatch(/cwd \/tmp\/nested\/.*…/);
	});
});
