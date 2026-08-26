import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { type Component, truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { getBundledInteractiveAssetPath, getPackageDir } from "../../../config.js";
import { theme } from "../theme/theme.js";

interface PrimeMethTiming {
	effect: "scanline-reveal";
	frameIntervalMs: number;
	durationMs: number;
}

interface PrimeMethPalette {
	name: string;
	truecolor: string[];
	ansiFallback: 34;
}

export interface PrimeMethBootAssets {
	frames: string[][];
	palette: PrimeMethPalette;
	timing: PrimeMethTiming;
}

export interface PrimeMethBootHeaderOptions {
	topPadding?: boolean;
	verboseInstructions?: string;
	getHideStartHint?: () => boolean;
	getStartHint?: () => string;
	getExtraMetadata?: () => Array<{ label: string; value: string }>;
	requestRender?: () => void;
	onAnimationStop?: () => void;
	animate?: boolean;
	deferAnimation?: boolean;
	assetDir?: string;
	clock?: () => number;
}

export interface PrimeMethAnimationEnvironment {
	animate?: boolean;
	stdinIsTTY?: boolean;
	stdoutIsTTY?: boolean;
	colorMode: "truecolor" | "256color";
}

const REVIEW_ASSET_DIR = resolve(getPackageDir(), "../..", "assets", "prime-meth");

export const PRIME_METH_ASSET_DIR = existsSync(getBundledInteractiveAssetPath("prime-meth"))
	? getBundledInteractiveAssetPath("prime-meth")
	: REVIEW_ASSET_DIR;

const activeAnimations = new Set<PrimeMethBootHeader>();

function parseJson(path: string): unknown {
	return JSON.parse(readFileSync(path, "utf8")) as unknown;
}

function isTiming(value: unknown): value is PrimeMethTiming {
	if (!value || typeof value !== "object") return false;
	const candidate = value as Record<string, unknown>;
	return (
		candidate.effect === "scanline-reveal" &&
		typeof candidate.frameIntervalMs === "number" &&
		candidate.frameIntervalMs >= 67 &&
		candidate.frameIntervalMs <= 100 &&
		typeof candidate.durationMs === "number" &&
		candidate.durationMs > 0 &&
		candidate.durationMs <= 2000
	);
}

function isPalette(value: unknown): value is PrimeMethPalette {
	if (!value || typeof value !== "object") return false;
	const candidate = value as Record<string, unknown>;
	return (
		typeof candidate.name === "string" &&
		Array.isArray(candidate.truecolor) &&
		candidate.truecolor.length > 0 &&
		candidate.truecolor.every((color) => typeof color === "string" && /^#[0-9a-fA-F]{6}$/.test(color)) &&
		candidate.ansiFallback === 34
	);
}

function parseFrames(text: string): string[][] {
	const frames: string[][] = [];
	let current: string[] | undefined;
	for (const line of text.replace(/\r\n/g, "\n").split("\n")) {
		if (/^--- frame \d+ ---$/.test(line)) {
			current = [];
			frames.push(current);
		} else if (current && (line.length > 0 || current.length > 0)) {
			current.push(line);
		}
	}
	for (const frame of frames) {
		while (frame.at(-1) === "") frame.pop();
	}
	if (frames.length < 2 || frames.some((frame) => frame.length === 0)) {
		throw new Error("Prime METH frames must contain at least two nonempty annotated frames");
	}
	const height = frames[0]!.length;
	const width = visibleWidth(frames[0]![0]!);
	if (frames.some((frame) => frame.length !== height || frame.some((line) => visibleWidth(line) !== width))) {
		throw new Error("Prime METH frames must have identical visible geometry");
	}
	return frames;
}

export function loadPrimeMethBootAssets(assetDir = PRIME_METH_ASSET_DIR): PrimeMethBootAssets {
	const timing = parseJson(join(assetDir, "timing.json"));
	const palette = parseJson(join(assetDir, "palette.json"));
	if (!isTiming(timing)) throw new Error("Prime METH timing.json is invalid");
	if (!isPalette(palette)) throw new Error("Prime METH palette.json is invalid");
	const frames = parseFrames(readFileSync(join(assetDir, "frames.txt"), "utf8"));
	if (frames.length !== Math.ceil(timing.durationMs / timing.frameIntervalMs)) {
		throw new Error("Prime METH frame count must cover the configured animation cadence");
	}
	return {
		frames,
		palette,
		timing,
	};
}

function fallbackAssets(): PrimeMethBootAssets {
	return {
		frames: [["Prime METH"]],
		palette: { name: "ANSI blue fallback", truecolor: ["#4f86f7"], ansiFallback: 34 },
		timing: { effect: "scanline-reveal", frameIntervalMs: 80, durationMs: 1920 },
	};
}

export function shouldAnimatePrimeMethBoot(environment: PrimeMethAnimationEnvironment): boolean {
	if (environment.animate !== undefined) return environment.animate;
	return environment.stdinIsTTY === true && environment.stdoutIsTTY === true && environment.colorMode === "truecolor";
}

export function primeMethFrameIndex(elapsedMs: number, assets: PrimeMethBootAssets): number {
	const progress = Math.min(1, Math.max(0, elapsedMs) / assets.timing.durationMs);
	return Math.min(assets.frames.length - 1, Math.floor(progress * assets.frames.length));
}

function formatCwd(cwd: string): string {
	const home = homedir();
	return home && cwd.startsWith(`${home}/`) ? `~${cwd.slice(home.length)}` : cwd;
}

function foreground(hex: string, text: string): string {
	const r = Number.parseInt(hex.slice(1, 3), 16);
	const g = Number.parseInt(hex.slice(3, 5), 16);
	const b = Number.parseInt(hex.slice(5, 7), 16);
	return `\x1b[38;2;${r};${g};${b}m${text}\x1b[39m`;
}

export function skipPrimeMethBootAnimations(): boolean {
	const animations = [...activeAnimations];
	for (const animation of animations) animation.skip();
	return animations.length > 0;
}

export function isPrimeMethTerminalCellSizeResponse(data: string): boolean {
	return /^\x1b\[6;\d+;\d+t$/.test(data);
}

export function consumePrimeMethBootInput(data: string): { consume: true } | undefined {
	if (isPrimeMethTerminalCellSizeResponse(data)) return undefined;
	return skipPrimeMethBootAnimations() ? { consume: true } : undefined;
}

export class PrimeMethBootHeader implements Component {
	private readonly assets: PrimeMethBootAssets;
	private readonly clock: () => number;
	private startedAt = 0;
	private frameIndex: number;
	private timer: ReturnType<typeof setInterval> | undefined;

	constructor(
		private readonly version: string,
		private readonly getModelId: () => string | undefined,
		private readonly getCwd: () => string,
		private readonly options: PrimeMethBootHeaderOptions = {},
	) {
		try {
			this.assets = loadPrimeMethBootAssets(options.assetDir);
		} catch {
			this.assets = fallbackAssets();
		}
		this.clock = options.clock ?? Date.now;
		this.frameIndex = this.assets.frames.length - 1;
		if (!options.deferAnimation) this.startAnimation();
	}

	startAnimation(animationOverride = this.options.animate): void {
		if (this.timer || this.assets.frames.length < 2) return;
		const shouldAnimate = shouldAnimatePrimeMethBoot({
			animate: animationOverride,
			stdinIsTTY: process.stdin.isTTY,
			stdoutIsTTY: process.stdout.isTTY,
			colorMode: theme.colorMode,
		});
		if (shouldAnimate) {
			this.startedAt = this.clock();
			this.frameIndex = 0;
			activeAnimations.add(this);
			this.timer = setInterval(() => this.advance(), this.assets.timing.frameIntervalMs);
			this.timer.unref?.();
		}
	}

	invalidate(): void {
		// All content is derived from current frame and live session getters.
	}

	skip(): void {
		this.frameIndex = this.assets.frames.length - 1;
		this.stopAnimation();
		this.options.requestRender?.();
	}

	dispose(): void {
		this.stopAnimation();
	}

	isAnimating(): boolean {
		return this.timer !== undefined;
	}

	render(width: number): string[] {
		const safeWidth = Math.max(1, width);
		const lines = this.options.topPadding ? [""] : [];
		const frame = this.assets.frames[this.frameIndex] ?? this.assets.frames.at(-1)!;
		for (const [index, rawLine] of frame.entries()) {
			const line = truncateToWidth(rawLine, safeWidth, "");
			const centered = " ".repeat(Math.max(0, Math.floor((safeWidth - visibleWidth(line)) / 2))) + line;
			const color = this.assets.palette.truecolor[index % this.assets.palette.truecolor.length]!;
			lines.push(
				theme.colorMode === "truecolor"
					? foreground(color, centered)
					: `\x1b[${this.assets.palette.ansiFallback}m${centered}\x1b[39m`,
			);
		}
		const metadata = [
			`Prime METH  version v${this.version}`,
			`model ${this.getModelId() ?? "—"}  cwd ${formatCwd(this.getCwd())}`,
			...(this.options.getExtraMetadata?.().map(({ label, value }) => `${label} ${value}`) ?? []),
			...(this.options.getHideStartHint?.() ? [] : [this.options.getStartHint?.() ?? "type to begin"]),
		];
		for (const line of metadata) {
			lines.push(theme.fg("dim", truncateToWidth(line, safeWidth, "")));
		}
		if (this.options.verboseInstructions) {
			lines.push(
				...this.options.verboseInstructions.split("\n").map((line) => truncateToWidth(line, safeWidth, "")),
			);
		}
		return lines;
	}

	private advance(): void {
		const elapsed = Math.max(0, this.clock() - this.startedAt);
		this.frameIndex = primeMethFrameIndex(elapsed, this.assets);
		if (elapsed >= this.assets.timing.durationMs) this.stopAnimation();
		this.options.requestRender?.();
	}

	private stopAnimation(): void {
		const wasAnimating = this.timer !== undefined;
		if (this.timer) clearInterval(this.timer);
		this.timer = undefined;
		activeAnimations.delete(this);
		if (wasAnimating) this.options.onAnimationStop?.();
	}
}
