# Synth configuration reference

This file is generated from the TypeBox schema in `.prime/agent/extensions/synth/config.ts`. Run `node_modules/.bin/tsx scripts/generate-synth-config-doc.ts` after changing the schema; `--check` fails when this reference is stale.

Synth reads one project-level `synth.json`. If the file is absent, the extension runs with all Synth features off. The defaults below apply after a valid file is present and a section is omitted. The repository’s shipped `synth.json` is an explicit local-development profile and may override them.

| Setting | Type | Required | Effective default | Constraints | Description |
| --- | --- | --- | --- | --- | --- |
| `endpoint.baseUrl` | string | when endpoint is present | none | min length 1 | Configured OpenAI-compatible endpoint base URL. |
| `endpoint.model` | string | when endpoint is present | none | min length 1 | Primary endpoint model identifier. |
| `endpoint.smallModel` | string | when endpoint is present | none | min length 1 | Endpoint model used for judging, verification, and summaries. |
| `endpoint.apiKeyEnv` | string | when endpoint is present | none | min length 1 | Environment variable containing the endpoint API key. |
| `endpoint.api` | openai-completions \| openai-responses | no | `"openai-completions"` | none | OpenAI-compatible protocol used by Synth control-plane model calls; defaults to Chat Completions. |
| `telemetry.dir` | string | when telemetry is present | `".synth/telemetry"` | min length 1 | Append-only Synth telemetry directory inside .synth. |
| `budgets.bash` | integer | when budgets is present | `0` | min 0 | Per-session bash tool-call ceiling; zero is unlimited. |
| `budgets.edit` | integer | when budgets is present | `0` | min 0 | Per-session edit tool-call ceiling; zero is unlimited. |
| `budgets.ipython` | integer | when budgets is present | `0` | min 0 | Per-session IPython tool-call ceiling; zero is unlimited. |
| `budgets.subagents` | integer | when budgets is present | `0` | min 0 | Per-session subagent-spawn ceiling; zero is unlimited. |
| `budgets.tokens` | integer | when budgets is present | `0` | min 0 | Native token-usage ceiling; zero is unlimited. |
| `budgets.costUsd` | number | when budgets is present | `0` | min 0 | Native cost ceiling in USD; zero is unlimited. |
| `budgets.elideToolResponsesAfterTurns` | integer | no | `3` | min 0, max 100 | Turn age after which old tool responses are omitted; zero disables elision. |
| `budgets.noneAnswerThreshold` | integer | no | `3` | min 1, max 20 | Consecutive no-tool answers before a remaining-budget continuation nudge. |
| `memory.enabled` | boolean | when memory is present | `false` | none | Enable ReasoningBank retrieval and shutdown distillation. |
| `memory.k` | integer | when memory is present | `3` | min 1, max 20 | Maximum retrieved items per memory class. |
| `memory.minScore` | number | when memory is present | `0.35` | min 0, max 1 | Minimum cosine similarity for retrieval. |
| `memory.db` | string | when memory is present | `".synth/synth.db"` | min length 1 | SQLite database path inside .synth. |
| `memory.embedModel` | string | when memory is present | `"BAAI/bge-small-en-v1.5"` | min length 1 | Pinned FastEmbed model identifier. |
| `memory.judgeModel` | string | when memory is present | `"small"` | min length 1 | Endpoint model selector used by the trajectory judge. |
| `memory.maxTrajectoryTokens` | integer | no | `64000` | min 1000, max 200000 | Conservative tokenizer-free trajectory ceiling for ReasoningBank judge and distillation prompts. |
| `verifier.interactive` | boolean | when verifier is present | `false` | none | Run strategic verification in interactive sessions. |
| `verifier.batch` | boolean | when verifier is present | `true` | none | Run strategic verification in Synth batch sessions. |
| `verifier.maxCycles` | integer | when verifier is present | `2` | min 0, max 10 | Maximum CONTINUE or PIVOT guidance cycles. |
| `verifier.contGen.enabled` | boolean | when verifier is present | `true` | none | Allow fresh trajectories after verified success. |
| `verifier.contGen.minBudgetFrac` | number | when verifier is present | `0.2` | min 0, max 1 | Minimum remaining budget fraction for another fresh trajectory. |
| `optimizer.config` | string | when optimizer is present | `"optimizer/configs/synth_local.yaml"` | min length 1 | AutoSaddler v2 scenario configuration path. |
| `skillCompiler.enabled` | boolean | when skillCompiler is present | `false` | none | Enable runtime skill compilation requests. |
| `skillCompiler.maxPerSession` | integer | when skillCompiler is present | `2` | min 0, max 20 | Maximum admitted compiler requests per session. |
| `skillCompiler.quarantine` | string | when skillCompiler is present | `".synth/quarantine"` | min length 1 | Compiler quarantine path inside .synth. |
| `killSwitch.all` | boolean | when killSwitch is present | `false` | none | Disable all Synth runtime features when true. |

## Operational semantics

- A zero tool, token, or cost budget means unlimited; positive values are enforced per session.
- `telemetry.dir`, `memory.db`, and `skillCompiler.quarantine` must resolve inside the workspace’s `.synth/` data plane.
- `endpoint` is optional. Features needing model inference degrade locally when it is absent or unavailable; the agent session continues.
- `killSwitch.all` disables every Synth runtime feature without changing the Prime Agent control plane.
- Runtime skill changes are confined to `.prime/agent/skills/`, `.synth/`, Synth SQLite state, and native HarnessState data. Control-plane changes are optimizer-only and validation-gated.
