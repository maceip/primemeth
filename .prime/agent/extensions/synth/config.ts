import { existsSync, readFileSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import { type Static, Type } from "typebox";
import { Value } from "typebox/value";

const EndpointApiSchema = Type.Union(
	[Type.Literal("openai-completions"), Type.Literal("openai-responses")],
	{
		description:
			"OpenAI-compatible protocol used by Synth control-plane model calls; defaults to Chat Completions.",
		default: "openai-completions",
	},
);

const EndpointSchema = Type.Object(
	{
		baseUrl: Type.String({
			minLength: 1,
			description: "Configured OpenAI-compatible endpoint base URL.",
		}),
		model: Type.String({ minLength: 1, description: "Primary endpoint model identifier." }),
		smallModel: Type.String({
			minLength: 1,
			description: "Endpoint model used for judging, verification, and summaries.",
		}),
		apiKeyEnv: Type.String({
			minLength: 1,
			description: "Environment variable containing the endpoint API key.",
		}),
		api: Type.Optional(EndpointApiSchema),
	},
	{ additionalProperties: false },
);

const TelemetryConfigSchema = Type.Object(
	{
		dir: Type.String({
			minLength: 1,
			description: "Append-only Synth telemetry directory inside .synth.",
		}),
	},
	{ additionalProperties: false },
);

const BudgetsSchema = Type.Object(
	{
		bash: Type.Integer({ minimum: 0, description: "Per-session bash tool-call ceiling; zero is unlimited." }),
		edit: Type.Integer({ minimum: 0, description: "Per-session edit tool-call ceiling; zero is unlimited." }),
		ipython: Type.Integer({
			minimum: 0,
			description: "Per-session IPython tool-call ceiling; zero is unlimited.",
		}),
		subagents: Type.Integer({
			minimum: 0,
			description: "Per-session subagent-spawn ceiling; zero is unlimited.",
		}),
		tokens: Type.Integer({ minimum: 0, description: "Native token-usage ceiling; zero is unlimited." }),
		costUsd: Type.Number({ minimum: 0, description: "Native cost ceiling in USD; zero is unlimited." }),
		elideToolResponsesAfterTurns: Type.Optional(
			Type.Integer({
				minimum: 0,
				maximum: 100,
				description: "Turn age after which old tool responses are omitted; zero disables elision.",
			}),
		),
		noneAnswerThreshold: Type.Optional(
			Type.Integer({
				minimum: 1,
				maximum: 20,
				description: "Consecutive no-tool answers before a remaining-budget continuation nudge.",
			}),
		),
	},
	{ additionalProperties: false },
);

export interface SynthBudgetConfig {
	bash: number;
	edit: number;
	ipython: number;
	subagents: number;
	tokens: number;
	costUsd: number;
	elideToolResponsesAfterTurns: number;
	noneAnswerThreshold: number;
}

const MemorySchema = Type.Object(
	{
		enabled: Type.Boolean({ description: "Enable ReasoningBank retrieval and shutdown distillation." }),
		k: Type.Integer({ minimum: 1, maximum: 20, description: "Maximum retrieved items per memory class." }),
		minScore: Type.Number({ minimum: 0, maximum: 1, description: "Minimum cosine similarity for retrieval." }),
		db: Type.String({ minLength: 1, description: "SQLite database path inside .synth." }),
		embedModel: Type.String({ minLength: 1, description: "Pinned FastEmbed model identifier." }),
		judgeModel: Type.String({ minLength: 1, description: "Endpoint model selector used by the trajectory judge." }),
		maxTrajectoryTokens: Type.Optional(
			Type.Integer({
				minimum: 1_000,
				maximum: 200_000,
				description:
					"Conservative tokenizer-free trajectory ceiling for ReasoningBank judge and distillation prompts.",
			}),
		),
	},
	{ additionalProperties: false },
);

const VerifierSchema = Type.Object(
	{
		interactive: Type.Boolean({ description: "Run strategic verification in interactive sessions." }),
		batch: Type.Boolean({ description: "Run strategic verification in Synth batch sessions." }),
		maxCycles: Type.Integer({ minimum: 0, maximum: 10, description: "Maximum CONTINUE or PIVOT guidance cycles." }),
		contGen: Type.Object(
			{
				enabled: Type.Boolean({ description: "Allow fresh trajectories after verified success." }),
				minBudgetFrac: Type.Number({
					minimum: 0,
					maximum: 1,
					description: "Minimum remaining budget fraction for another fresh trajectory.",
				}),
			},
			{ additionalProperties: false },
		),
	},
	{ additionalProperties: false },
);

const OptimizerSchema = Type.Object(
	{ config: Type.String({ minLength: 1, description: "AutoSaddler v2 scenario configuration path." }) },
	{ additionalProperties: false },
);

const SkillCompilerSchema = Type.Object(
	{
		enabled: Type.Boolean({ description: "Enable runtime skill compilation requests." }),
		maxPerSession: Type.Integer({ minimum: 0, maximum: 20, description: "Maximum admitted compiler requests per session." }),
		quarantine: Type.String({ minLength: 1, description: "Compiler quarantine path inside .synth." }),
	},
	{ additionalProperties: false },
);

const KillSwitchSchema = Type.Object(
	{ all: Type.Boolean({ description: "Disable all Synth runtime features when true." }) },
	{ additionalProperties: false },
);

const QwenModeSchema = Type.Union([Type.Literal("thinking"), Type.Literal("non-thinking")], {
	description: "Qwen sampling mode.",
});

const QwenContextProfileSchema = Type.Union([Type.Literal("native"), Type.Literal("yarn-1m")], {
	description: "Qwen context profile (native 262,144 vs opt-in yarn-1m).",
});

const QwenConfigSchema = Type.Object(
	{
		mode: QwenModeSchema,
		contextProfile: QwenContextProfileSchema,
	},
	{ additionalProperties: false },
);

export const SynthConfigSchema = Type.Object(
	{
		endpoint: Type.Optional(EndpointSchema),
		telemetry: Type.Optional(TelemetryConfigSchema),
		budgets: Type.Optional(BudgetsSchema),
		memory: Type.Optional(MemorySchema),
		verifier: Type.Optional(VerifierSchema),
		optimizer: Type.Optional(OptimizerSchema),
		skillCompiler: Type.Optional(SkillCompilerSchema),
		killSwitch: Type.Optional(KillSwitchSchema),
		qwen: Type.Optional(QwenConfigSchema),
	},
	{ additionalProperties: false },
);

export type SynthConfigFile = Static<typeof SynthConfigSchema>;
export type SynthEndpointApi = Static<typeof EndpointApiSchema>;
export type QwenMode = Static<typeof QwenModeSchema>;
export type QwenContextProfile = Static<typeof QwenContextProfileSchema>;
export type QwenConfig = Static<typeof QwenConfigSchema>;

export interface SynthEndpointConfig {
	baseUrl: string;
	model: string;
	smallModel: string;
	apiKeyEnv: string;
	api: SynthEndpointApi;
}

export type SynthMemoryConfig = Omit<Static<typeof MemorySchema>, "maxTrajectoryTokens"> & {
	maxTrajectoryTokens: number;
};

export interface SynthConfig {
	endpoint?: SynthEndpointConfig;
	telemetry: Static<typeof TelemetryConfigSchema>;
	budgets: SynthBudgetConfig;
	memory: SynthMemoryConfig;
	verifier: Static<typeof VerifierSchema>;
	optimizer: Static<typeof OptimizerSchema>;
	skillCompiler: Static<typeof SkillCompilerSchema>;
	killSwitch: Static<typeof KillSwitchSchema>;
	qwen?: QwenConfig;
}

export interface SynthConfigLoadResult {
	path: string;
	state: "absent" | "invalid" | "loaded";
	config?: SynthConfig;
	error?: string;
}

export const QWEN_SAMPLING: Record<QwenMode, {
	temperature: number;
	top_p: number;
	top_k: number;
	min_p: number;
	presence_penalty: number;
	repetition_penalty: number;
}> = {
	thinking: {
		temperature: 1.0,
		top_p: 0.95,
		top_k: 20,
		min_p: 0.0,
		presence_penalty: 0.0,
		repetition_penalty: 1.0,
	},
	"non-thinking": {
		temperature: 0.7,
		top_p: 0.80,
		top_k: 20,
		min_p: 0.0,
		presence_penalty: 1.5,
		repetition_penalty: 1.0,
	},
};

/**
 * Model-card YaRN recipe (opt-in) override for 1M context:
 * Targets text_config.rope_parameters while preserving shipped keys
 * (mrope_interleaved, mrope_section, partial_rotary_factor, rope_theta)
 * and setting rope_type='yarn', factor=4.0, original_max_position_embeddings=262144 (262144 x 4 = 1M).
 * Native profile emits nothing (shipped config untouched).
 */
export const QWEN_YARN_1M_ROPE_OVERRIDE = {
	text_config: {
		rope_parameters: {
			mrope_interleaved: true,
			mrope_section: [11, 11, 10],
			partial_rotary_factor: 0.25,
			rope_theta: 10000000,
			rope_type: "yarn",
			factor: 4.0,
			original_max_position_embeddings: 262144,
		},
	},
} as const;

export const SYNTH_CONFIG_DEFAULTS: Omit<SynthConfig, "endpoint"> = {
	telemetry: { dir: ".synth/telemetry" },
	budgets: {
		bash: 0,
		edit: 0,
		ipython: 0,
		subagents: 0,
		tokens: 0,
		costUsd: 0,
		elideToolResponsesAfterTurns: 3,
		noneAnswerThreshold: 3,
	},
	memory: {
		enabled: false,
		k: 3,
		minScore: 0.35,
		db: ".synth/synth.db",
		embedModel: "BAAI/bge-small-en-v1.5",
		judgeModel: "small",
		maxTrajectoryTokens: 64_000,
	},
	verifier: {
		interactive: false,
		batch: true,
		maxCycles: 2,
		contGen: { enabled: true, minBudgetFrac: 0.2 },
	},
	optimizer: { config: "optimizer/configs/synth_local.yaml" },
	skillCompiler: { enabled: false, maxPerSession: 2, quarantine: ".synth/quarantine" },
	killSwitch: { all: false },
};

function isWithin(parent: string, candidate: string): boolean {
	const pathFromParent = relative(parent, candidate);
	return pathFromParent === "" || (!pathFromParent.startsWith("..") && !isAbsolute(pathFromParent));
}

export function resolveDataPlanePath(cwd: string, configuredPath: string): string {
	const synthDataRoot = resolve(cwd, ".synth");
	const candidate = resolve(cwd, configuredPath);
	if (!isWithin(synthDataRoot, candidate)) {
		throw new Error(`data-plane path must remain inside ${synthDataRoot}: ${configuredPath}`);
	}
	return candidate;
}

function normalizeConfig(cwd: string, value: SynthConfigFile): SynthConfig {
	const config: SynthConfig = {
		...(value.endpoint
			? { endpoint: { ...value.endpoint, api: value.endpoint.api ?? "openai-completions" } }
			: {}),
		telemetry: value.telemetry ?? SYNTH_CONFIG_DEFAULTS.telemetry,
		budgets: { ...SYNTH_CONFIG_DEFAULTS.budgets, ...value.budgets },
		memory: { ...SYNTH_CONFIG_DEFAULTS.memory, ...value.memory },
		verifier: value.verifier ?? SYNTH_CONFIG_DEFAULTS.verifier,
		optimizer: value.optimizer ?? SYNTH_CONFIG_DEFAULTS.optimizer,
		skillCompiler: value.skillCompiler ?? SYNTH_CONFIG_DEFAULTS.skillCompiler,
		killSwitch: value.killSwitch ?? SYNTH_CONFIG_DEFAULTS.killSwitch,
		...(value.qwen ? { qwen: value.qwen } : {}),
	};
	resolveDataPlanePath(cwd, config.telemetry.dir);
	resolveDataPlanePath(cwd, config.memory.db);
	resolveDataPlanePath(cwd, config.skillCompiler.quarantine);
	return config;
}

export function loadSynthConfig(cwd: string): SynthConfigLoadResult {
	const path = join(cwd, "synth.json");
	if (!existsSync(path)) {
		return { path, state: "absent" };
	}
	try {
		const pathStat = statSync(path);
		if (!pathStat.isFile()) {
			throw new Error("config is not a regular file");
		}
		const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
		if (!Value.Check(SynthConfigSchema, parsed)) {
			const detail = [...Value.Errors(SynthConfigSchema, parsed)]
				.map((error) => `${error.instancePath || "/"}: ${error.message}`)
				.join("; ");
			throw new Error(detail || "config does not match the Synth schema");
		}
		return { path, state: "loaded", config: normalizeConfig(cwd, parsed) };
	} catch (error) {
		const detail = error instanceof Error ? error.message : String(error);
		return { path, state: "invalid", error: detail };
	}
}
