import type { Usage } from "@earendil-works/pi-ai";

export interface ProviderThroughput {
	ttftMs: number;
	generationMs: number;
	prefillTokens: number;
	decodeTokens: number;
	prefillTokensPerSecond?: number;
	decodeTokensPerSecond?: number;
}

function finiteDuration(later: number, earlier: number): number | undefined {
	const duration = later - earlier;
	return Number.isFinite(duration) && duration >= 0 ? duration : undefined;
}

export function computeProviderThroughput(
	usage: Usage,
	requestStartedAt: number,
	firstOutputAt: number,
	responseEndedAt: number,
): ProviderThroughput | undefined {
	const ttftMs = finiteDuration(firstOutputAt, requestStartedAt);
	const generationMs = finiteDuration(responseEndedAt, firstOutputAt);
	if (ttftMs === undefined || generationMs === undefined) return undefined;

	// Cache writes are provider-side prompt work; cache reads are reused work and stay out
	// of the prefill numerator. The first output token belongs to TTFT, not decode.
	const prefillTokens = Math.max(0, usage.input + usage.cacheWrite);
	const decodeTokens = Math.max(0, usage.output - 1);
	return {
		ttftMs,
		generationMs,
		prefillTokens,
		decodeTokens,
		...(prefillTokens > 0 && ttftMs > 0
			? { prefillTokensPerSecond: (prefillTokens * 1000) / ttftMs }
			: {}),
		...(decodeTokens > 0 && generationMs > 0
			? { decodeTokensPerSecond: (decodeTokens * 1000) / generationMs }
			: {}),
	};
}

function formatRate(value: number): string {
	if (value >= 1000) return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
	return value.toLocaleString("en-US", {
		minimumFractionDigits: value < 10 ? 1 : 0,
		maximumFractionDigits: 1,
	});
}

export function formatProviderThroughput(throughput: ProviderThroughput | undefined): string | undefined {
	if (!throughput) return undefined;
	const sections: string[] = [];
	if (throughput.prefillTokensPerSecond !== undefined) {
		sections.push(`prefill ${formatRate(throughput.prefillTokensPerSecond)} tok/s`);
	}
	if (throughput.decodeTokensPerSecond !== undefined) {
		sections.push(`decode ${formatRate(throughput.decodeTokensPerSecond)} tok/s`);
	}
	return sections.length > 0 ? sections.join("  ·  ") : undefined;
}
