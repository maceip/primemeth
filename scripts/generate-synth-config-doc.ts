import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
	SYNTH_CONFIG_DEFAULTS,
	SynthConfigSchema,
} from "../.prime/agent/extensions/synth/config.js";

interface ConfigRow {
	path: string;
	type: string;
	required: string;
	defaultValue: string;
	constraints: string;
	description: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function schemaProperties(schema: Record<string, unknown>): Record<string, unknown> {
	return isRecord(schema.properties) ? schema.properties : {};
}

function requiredNames(schema: Record<string, unknown>): Set<string> {
	return new Set(
		Array.isArray(schema.required)
			? schema.required.filter((value): value is string => typeof value === "string")
			: [],
	);
}

function schemaType(schema: Record<string, unknown>): string {
	if (Array.isArray(schema.anyOf)) {
		const literals = schema.anyOf.flatMap((entry) =>
			isRecord(entry) && typeof entry.const === "string" ? [entry.const] : [],
		);
		if (literals.length === schema.anyOf.length && literals.length > 0) return literals.join(" | ");
	}
	return typeof schema.type === "string" ? schema.type : "unknown";
}

function constraints(schema: Record<string, unknown>): string {
	const values: string[] = [];
	if (typeof schema.minimum === "number") values.push(`min ${schema.minimum}`);
	if (typeof schema.maximum === "number") values.push(`max ${schema.maximum}`);
	if (typeof schema.minLength === "number") values.push(`min length ${schema.minLength}`);
	return values.length > 0 ? values.join(", ") : "none";
}

function formatDefault(value: unknown, present: boolean): string {
	if (!present) return "none";
	return `\`${JSON.stringify(value)}\``;
}

function collectRows(
	schema: Record<string, unknown>,
	defaults: unknown,
	prefix: readonly string[] = [],
	optionalAncestor = false,
): ConfigRow[] {
	const rows: ConfigRow[] = [];
	const required = requiredNames(schema);
	const defaultObject = isRecord(defaults) ? defaults : {};
	for (const [name, rawChild] of Object.entries(schemaProperties(schema))) {
		if (!isRecord(rawChild)) continue;
		const path = [...prefix, name];
		const inheritedDefault = Object.hasOwn(defaultObject, name);
		const schemaDefault = Object.hasOwn(rawChild, "default");
		const presentByDefault = inheritedDefault || schemaDefault;
		const childDefault = inheritedDefault ? defaultObject[name] : rawChild.default;
		const childRequired = required.has(name);
		const childProperties = schemaProperties(rawChild);
		if (Object.keys(childProperties).length > 0) {
			rows.push(
				...collectRows(
					rawChild,
					childDefault,
					path,
					optionalAncestor || !childRequired,
				),
			);
			continue;
		}
		rows.push({
			path: path.join("."),
			type: schemaType(rawChild),
			required: childRequired
				? optionalAncestor
					? `when ${path[0]} is present`
					: "yes"
				: "no",
			defaultValue: formatDefault(childDefault, presentByDefault),
			constraints: constraints(rawChild),
			description:
				typeof rawChild.description === "string"
					? rawChild.description
					: "No description supplied by the schema.",
		});
	}
	return rows;
}

function escapeCell(value: string): string {
	return value.replaceAll("|", "\\|").replaceAll("\n", " ");
}

function renderDocument(rows: readonly ConfigRow[]): string {
	const table = rows.map(
		(row) =>
			`| \`${row.path}\` | ${escapeCell(row.type)} | ${escapeCell(row.required)} | ${escapeCell(row.defaultValue)} | ${escapeCell(row.constraints)} | ${escapeCell(row.description)} |`,
	);
	return `# Synth configuration reference

This file is generated from the TypeBox schema in \`.prime/agent/extensions/synth/config.ts\`. Run \`node_modules/.bin/tsx scripts/generate-synth-config-doc.ts\` after changing the schema; \`--check\` fails when this reference is stale.

Synth reads one project-level \`synth.json\`. If the file is absent, the extension runs with all Synth features off. The defaults below apply after a valid file is present and a section is omitted. The repository’s shipped \`synth.json\` is an explicit local-development profile and may override them.

| Setting | Type | Required | Effective default | Constraints | Description |
| --- | --- | --- | --- | --- | --- |
${table.join("\n")}

## Operational semantics

- A zero tool, token, or cost budget means unlimited; positive values are enforced per session.
- \`telemetry.dir\`, \`memory.db\`, and \`skillCompiler.quarantine\` must resolve inside the workspace’s \`.synth/\` data plane.
- \`endpoint\` is optional. Features needing model inference degrade locally when it is absent or unavailable; the agent session continues.
- \`killSwitch.all\` disables every Synth runtime feature without changing the Prime Agent control plane.
- Runtime skill changes are confined to \`.prime/agent/skills/\`, \`.synth/\`, Synth SQLite state, and native HarnessState data. Control-plane changes are optimizer-only and validation-gated.
`;
}

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const outputPath = resolve(scriptDirectory, "..", "docs", "synth", "config.md");
const args = process.argv.slice(2);
if (args.some((arg) => arg !== "--check") || args.length > 1) {
	throw new Error("usage: generate-synth-config-doc.ts [--check]");
}
const output = renderDocument(
	collectRows(
		SynthConfigSchema as unknown as Record<string, unknown>,
		SYNTH_CONFIG_DEFAULTS,
	),
);
if (args[0] === "--check") {
	const current = readFileSync(outputPath, "utf8");
	if (current !== output) {
		throw new Error(`generated Synth config reference is stale: ${outputPath}`);
	}
	console.log(`synth-config-doc: current ${outputPath}`);
} else {
	mkdirSync(dirname(outputPath), { recursive: true });
	writeFileSync(outputPath, output);
	console.log(`synth-config-doc: wrote ${outputPath}`);
}
