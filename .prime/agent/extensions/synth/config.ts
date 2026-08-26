import { existsSync, readFileSync, statSync } from "node:fs";
import { isAbsolute, join, relative, resolve } from "node:path";
import { type Static, Type } from "typebox";
import { Value } from "typebox/value";

const EndpointSchema = Type.Object(
	{
		baseUrl: Type.String({ minLength: 1 }),
		model: Type.String({ minLength: 1 }),
		smallModel: Type.String({ minLength: 1 }),
		apiKeyEnv: Type.String({ minLength: 1 }),
	},
	{ additionalProperties: false },
);

const TelemetryConfigSchema = Type.Object(
	{ dir: Type.String({ minLength: 1 }) },
	{ additionalProperties: false },
);

const BudgetsSchema = Type.Object(
	{
		bash: Type.Integer({ minimum: 0 }),
		edit: Type.Integer({ minimum: 0 }),
		ipython: Type.Integer({ minimum: 0 }),
		subagents: Type.Integer({ minimum: 0 }),
		tokens: Type.Integer({ minimum: 0 }),
		costUsd: Type.Number({ minimum: 0 }),
	},
	{ additionalProperties: false },
);

const MemorySchema = Type.Object(
	{
		enabled: Type.Boolean(),
		k: Type.Integer({ minimum: 1, maximum: 20 }),
		minScore: Type.Number({ minimum: 0, maximum: 1 }),
		db: Type.String({ minLength: 1 }),
		embedModel: Type.String({ minLength: 1 }),
		judgeModel: Type.String({ minLength: 1 }),
	},
	{ additionalProperties: false },
);

const VerifierSchema = Type.Object(
	{
		interactive: Type.Boolean(),
		batch: Type.Boolean(),
		maxCycles: Type.Integer({ minimum: 0, maximum: 10 }),
		contGen: Type.Object(
			{
				enabled: Type.Boolean(),
				minBudgetFrac: Type.Number({ minimum: 0, maximum: 1 }),
			},
			{ additionalProperties: false },
		),
	},
	{ additionalProperties: false },
);

const OptimizerSchema = Type.Object(
	{ config: Type.String({ minLength: 1 }) },
	{ additionalProperties: false },
);

const SkillCompilerSchema = Type.Object(
	{
		enabled: Type.Boolean(),
		maxPerSession: Type.Integer({ minimum: 0, maximum: 20 }),
		quarantine: Type.String({ minLength: 1 }),
	},
	{ additionalProperties: false },
);

const KillSwitchSchema = Type.Object(
	{ all: Type.Boolean() },
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
	},
	{ additionalProperties: false },
);

export type SynthConfigFile = Static<typeof SynthConfigSchema>;

export interface SynthConfig {
	endpoint?: Static<typeof EndpointSchema>;
	telemetry: Static<typeof TelemetryConfigSchema>;
	budgets: Static<typeof BudgetsSchema>;
	memory: Static<typeof MemorySchema>;
	verifier: Static<typeof VerifierSchema>;
	optimizer: Static<typeof OptimizerSchema>;
	skillCompiler: Static<typeof SkillCompilerSchema>;
	killSwitch: Static<typeof KillSwitchSchema>;
}

export interface SynthConfigLoadResult {
	path: string;
	state: "absent" | "invalid" | "loaded";
	config?: SynthConfig;
	error?: string;
}

const DEFAULT_CONFIG: Omit<SynthConfig, "endpoint"> = {
	telemetry: { dir: ".synth/telemetry" },
	budgets: { bash: 0, edit: 0, ipython: 0, subagents: 0, tokens: 0, costUsd: 0 },
	memory: {
		enabled: false,
		k: 3,
		minScore: 0.35,
		db: ".synth/synth.db",
		embedModel: "BAAI/bge-small-en-v1.5",
		judgeModel: "small",
	},
	verifier: {
		interactive: false,
		batch: false,
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
		...(value.endpoint ? { endpoint: value.endpoint } : {}),
		telemetry: value.telemetry ?? DEFAULT_CONFIG.telemetry,
		budgets: value.budgets ?? DEFAULT_CONFIG.budgets,
		memory: value.memory ?? DEFAULT_CONFIG.memory,
		verifier: value.verifier ?? DEFAULT_CONFIG.verifier,
		optimizer: value.optimizer ?? DEFAULT_CONFIG.optimizer,
		skillCompiler: value.skillCompiler ?? DEFAULT_CONFIG.skillCompiler,
		killSwitch: value.killSwitch ?? DEFAULT_CONFIG.killSwitch,
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
