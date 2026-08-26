import type { EditorTheme, TUI } from "@earendil-works/pi-tui";
import { CURSOR_MARKER, setKeybindings, visibleWidth } from "@earendil-works/pi-tui";
import stripAnsi from "strip-ansi";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
	computeProviderThroughput,
	formatProviderThroughput,
} from "../../../.prime/agent/extensions/synth/throughput.js";
import { KeybindingsManager } from "../src/core/keybindings.js";
import { CustomEditor } from "../src/modes/interactive/components/custom-editor.js";
import {
	consumePrimeMethBootInput,
	isPrimeMethTerminalCellSizeResponse,
	loadPrimeMethBootAssets,
	PRIME_METH_ASSET_DIR,
	PrimeMethBootHeader,
	primeMethFrameIndex,
	shouldAnimatePrimeMethBoot,
} from "../src/modes/interactive/components/prime-meth-boot.js";
import { initTheme } from "../src/modes/interactive/theme/theme.js";
import { formatPrimeMethPrompt, PRIME_METH_PROMPT } from "../src/themes/prime-meth.js";

const passthrough = (text: string) => text;
const editorTheme: EditorTheme = {
	borderColor: passthrough,
	selectList: {
		selectedPrefix: passthrough,
		selectedText: passthrough,
		description: passthrough,
		scrollInfo: passthrough,
		noMatch: passthrough,
	},
};
const fakeTui = {
	requestRender: vi.fn(),
	terminal: { rows: 24, columns: 80 },
} as unknown as TUI;

describe("Prime METH branding", () => {
	beforeAll(() => {
		initTheme("dark");
	});

	afterEach(() => {
		vi.useRealTimers();
	});

	it("formats the exact per-turn prompt with truecolor and ANSI-blue modes", () => {
		const truecolor = formatPrimeMethPrompt("truecolor");
		const fallback = formatPrimeMethPrompt("256color");

		expect(stripAnsi(truecolor)).toBe(PRIME_METH_PROMPT);
		expect(truecolor.match(/\x1b\[38;2;/g)).toHaveLength(4);
		expect(fallback).toContain("\x1b[34mMETH\x1b[39m");
		expect(stripAnsi(fallback)).toBe(PRIME_METH_PROMPT);
	});

	it("places the branded prompt before the caret only when the session opts in", () => {
		setKeybindings(new KeybindingsManager());
		const branded = new CustomEditor(fakeTui, editorTheme, new KeybindingsManager(), {
			promptPrefix: PRIME_METH_PROMPT,
		});
		branded.focused = true;
		branded.setText("inspect");
		const brandedRender = branded.render(80).join("\n");

		expect(stripAnsi(brandedRender)).toContain("prime METH> inspect");
		expect(brandedRender).toContain(CURSOR_MARKER);

		const ordinary = new CustomEditor(fakeTui, editorTheme, new KeybindingsManager());
		ordinary.setText("inspect");
		expect(stripAnsi(ordinary.render(80).join("\n"))).toContain("> inspect");
		expect(stripAnsi(ordinary.render(80).join("\n"))).not.toContain("prime METH>");

		branded.setText("! echo safe");
		expect(stripAnsi(branded.render(80).join("\n"))).toContain("! echo safe");
		expect(stripAnsi(branded.render(80).join("\n"))).not.toContain("prime METH>");
	});

	it("loads 24 reviewable fixed-geometry frames for a 1.92 second scanline reveal", () => {
		const assets = loadPrimeMethBootAssets();
		const widths = new Set(assets.frames.flatMap((frame) => frame.map((line) => visibleWidth(line))));
		const heights = new Set(assets.frames.map((frame) => frame.length));

		expect(PRIME_METH_ASSET_DIR).toContain("prime-meth");
		expect(assets.timing).toEqual({ effect: "scanline-reveal", frameIntervalMs: 80, durationMs: 1920 });
		expect(assets.frames).toHaveLength(24);
		expect(widths).toEqual(new Set([69]));
		expect(heights).toEqual(new Set([8]));
		expect(new Set(assets.frames.map((frame) => frame.join("\n"))).size).toBe(24);
		for (let index = 0; index < 24; index++) {
			expect(primeMethFrameIndex(index * 80, assets)).toBe(index);
		}
	});

	it("animates every 80ms, settles by two seconds, and clears its timer", () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		const component = new PrimeMethBootHeader(
			"test",
			() => "faux",
			() => "/tmp/synth",
			{
				animate: true,
				clock: Date.now,
			},
		);
		const frames = [stripAnsi(component.render(80).slice(0, 8).join("\n"))];

		for (let index = 1; index < 24; index++) {
			vi.advanceTimersByTime(80);
			frames.push(stripAnsi(component.render(80).slice(0, 8).join("\n")));
		}
		expect(new Set(frames).size).toBe(24);

		vi.advanceTimersByTime(80);
		expect(vi.getTimerCount()).toBe(0);
		component.dispose();
	});

	it("can defer its animation clock until the owning UI is ready", () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		const component = new PrimeMethBootHeader(
			"test",
			() => "faux",
			() => "/tmp/synth",
			{
				animate: true,
				deferAnimation: true,
				clock: Date.now,
			},
		);
		const settled = stripAnsi(component.render(80).join("\n"));

		vi.advanceTimersByTime(2500);
		expect(vi.getTimerCount()).toBe(0);
		component.startAnimation();
		expect(vi.getTimerCount()).toBe(1);
		expect(stripAnsi(component.render(80).join("\n"))).not.toBe(settled);

		vi.advanceTimersByTime(1920);
		expect(vi.getTimerCount()).toBe(0);
		component.dispose();
	});

	it("consumes the key used to skip and leaves the editor ready for the next key", () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		setKeybindings(new KeybindingsManager());
		const component = new PrimeMethBootHeader(
			"test",
			() => "faux",
			() => "/tmp/synth",
			{
				animate: true,
				clock: Date.now,
			},
		);
		const before = stripAnsi(component.render(80).join("\n"));
		const editor = new CustomEditor(fakeTui, editorTheme, new KeybindingsManager(), {
			promptPrefix: PRIME_METH_PROMPT,
		});

		editor.handleInput("x");
		const after = stripAnsi(component.render(80).join("\n"));
		expect(editor.getText()).toBe("");
		expect(after).not.toBe(before);
		expect(vi.getTimerCount()).toBe(0);

		editor.handleInput("x");
		expect(editor.getText()).toBe("x");
		component.dispose();
	});

	it("falls back to a bounded static mark when assets are missing", () => {
		const component = new PrimeMethBootHeader(
			"test",
			() => undefined,
			() => "/tmp",
			{
				assetDir: "/definitely/missing/prime-meth",
				animate: true,
			},
		);
		expect(component.isAnimating()).toBe(false);
		for (const width of [20, 40, 69, 80, 120]) {
			const lines = component.render(width);
			expect(stripAnsi(lines.join("\n"))).toContain("Prime METH");
			for (const line of lines) expect(visibleWidth(line)).toBeLessThanOrEqual(width);
		}
		component.dispose();
	});

	it("animates only for a fully interactive truecolor terminal unless explicitly overridden", () => {
		expect(shouldAnimatePrimeMethBoot({ stdinIsTTY: true, stdoutIsTTY: true, colorMode: "truecolor" })).toBe(true);
		expect(shouldAnimatePrimeMethBoot({ stdinIsTTY: false, stdoutIsTTY: true, colorMode: "truecolor" })).toBe(false);
		expect(shouldAnimatePrimeMethBoot({ stdinIsTTY: true, stdoutIsTTY: true, colorMode: "256color" })).toBe(false);
		expect(
			shouldAnimatePrimeMethBoot({ animate: false, stdinIsTTY: true, stdoutIsTTY: true, colorMode: "truecolor" }),
		).toBe(false);
	});

	it("distinguishes the terminal cell-size probe from user input", () => {
		expect(isPrimeMethTerminalCellSizeResponse("\x1b[6;24;80t")).toBe(true);
		expect(isPrimeMethTerminalCellSizeResponse("pageup")).toBe(false);
		expect(isPrimeMethTerminalCellSizeResponse("\x1b[6;24;80tx")).toBe(false);
	});

	it("consumes pre-dispatch viewport input without treating a terminal probe as a keypress", () => {
		vi.useFakeTimers();
		vi.setSystemTime(0);
		const component = new PrimeMethBootHeader(
			"test",
			() => "faux",
			() => "/tmp/synth",
			{ animate: true, clock: Date.now },
		);

		expect(consumePrimeMethBootInput("\x1b[6;24;80t")).toBeUndefined();
		expect(component.isAnimating()).toBe(true);
		expect(consumePrimeMethBootInput("\x1b[5~")).toEqual({ consume: true });
		expect(component.isAnimating()).toBe(false);
		expect(consumePrimeMethBootInput("\x1b[5~")).toBeUndefined();
		component.dispose();
	});
});

describe("Synth provider throughput", () => {
	it("uses uncached prompt work for TTFT and post-first-token output for decode", () => {
		const throughput = computeProviderThroughput(
			{
				input: 90,
				output: 11,
				cacheRead: 50,
				cacheWrite: 10,
				totalTokens: 161,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
			0,
			1000,
			2000,
		);

		expect(throughput).toEqual({
			ttftMs: 1000,
			generationMs: 1000,
			prefillTokens: 100,
			decodeTokens: 10,
			prefillTokensPerSecond: 100,
			decodeTokensPerSecond: 10,
		});
		expect(formatProviderThroughput(throughput)).toBe("prefill 100 tok/s  ·  decode 10 tok/s");
	});

	it("omits rates when timing evidence is invalid or zero length", () => {
		const usage = {
			input: 1,
			output: 1,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 2,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		};
		expect(computeProviderThroughput(usage, 10, 5, 20)).toBeUndefined();
		expect(formatProviderThroughput(computeProviderThroughput(usage, 0, 0, 0))).toBeUndefined();
	});
});
