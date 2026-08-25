# Synth Architecture Notes

Phase 0 records the checked-out implementation at upstream commit `9bc00557489020e4dc981bef3111cb651c5955e7`. It is evidence for later phases, not a substitute for rechecking the source before each change.

## Repository snapshot

- Working branch: `synth/dev`
- Upstream remote: `https://github.com/PrimeIntellect-ai/prime-agent.git`
- Upstream baseline: `9bc005574`
- Verified toolchain: Node `v24.15.0`, npm `11.12.1`, uv `0.11.23`, host Python `3.14.6`.
- `node_modules/` was absent at initial recon. No Ollama, vLLM, or LM Studio command/process/listener was found; baseline model execution must therefore use faux.
- The Synth overlay arrived uncommitted: modified `.gitignore` and `README.md`; Python package scaffolds under `local_memory/`, `middleware/`, `optimizer/`, and `tests/`; root `pyproject.toml`.
- The overlay Python modules are docstring-only scaffolds in Phase 0. They are not represented as implemented features.
- The untracked scaffold database `local_memory/reasoning_bank.db` was removed. It was never in the index or local history, so the supplied "delete from tracking" description had drifted. Runtime databases belong under ignored `.synth/`.
- Reference snapshots fetched by `scripts/fetch-refs.sh`:
  - `refs/reasoning-bank` at `ed80611788292ea739f1effd31f16c53823b8a0d`
  - `refs/budget-aware-agent` at `37d23f66c512734db476313cf436a506edbec9e0`
  - `refs/AutoSaddler` at `30e20ce004486c58e7ee97c66182a8d0d41ec90e`

## Verified runtime topology

| Concern | Checked-out evidence | Consequence for Synth |
|---|---|---|
| Client/runtime boundary | `packages/coding-agent/docs/architecture.md`; `AgentConnection` fronts daemon-backed workers, while direct SDK integrations may run in-process. | Runtime integrations must work in interactive, print, and RPC paths without adding a second agent loop. |
| Session owner | `packages/coding-agent/src/core/agent-session.ts:1029` defines `AgentSession`; it owns provider calls, tools, compaction, child agents, and transcript writes. | Synchronous policy belongs in the TypeScript extension hooks around this loop. |
| Python boundary | `packages/coding-agent/docs/rlm-runtime.md`; TypeScript owns providers and child execution. `prime-agent-runtime` is a model-facing Python shim and makes no provider calls. | Embeddings, SQLite, distillation, and optimization can live in the overlay, but all turn-loop decisions remain in TypeScript. Python MCP helpers are network-capable and must remain disabled or locally configured under offline policy. |
| Extension API | `packages/coding-agent/src/core/extensions/types.ts:992`; lifecycle, prompt, context, provider, tool, refinement, command, tool, flag, and provider-registration APIs are present. | `.prime/agent/extensions/synth/index.ts` can be the only runtime TypeScript head. |
| Extension discovery | `discoverAndLoadExtensions()` starts at `packages/coding-agent/src/core/extensions/loader.ts:552`; the project directory is built at line 571. | `.prime/agent/extensions/synth/` is auto-discovered once it has an `index.ts` or a `pi.extensions` manifest. |
| Usage | `packages/ai/src/types.ts:202` defines native token, cache-read/cache-write, and cost accounting; `packages/coding-agent/src/core/usage.ts` adds and subtracts it. | BATS must consume native `Usage`; no overlay tokenizer is needed. |
| Session persistence | `packages/coding-agent/docs/session-format.md`; `~/.prime/agent/sessions/<id>.jsonl`, version 3, tree-linked entries. `session_state` and `agent_status` are defined in `session-manager.ts:179-195`. | Synth telemetry points back to the native transcript instead of duplicating it. |
| Headless outcome | `packages/coding-agent/src/modes/print-mode.ts:68-161` computes and returns a real nonzero result for provider, compaction, and autonomous-gate failures. | Batch runners and judges may use the exit code as a prior, never as the sole correctness signal. |
| Prompt assembly | `buildSystemPrompt()` is at `packages/coding-agent/src/core/system-prompt.ts:41`; HarnessState is formatted at `refinement.ts:429`. On the default-prompt path it precedes project context and installed skills; the custom-prompt path puts context and skills first. | Synth sections are appended in `before_agent_start`, clearly labeled and separate from HarnessState; tests must cover both base-prompt paths where ordering matters. |
| Refinement | `session_before_refine` and `refine_complete` are real extension hooks. Native state has prompt, memory, skill-description, and subagent entries. | Skill compilation may reuse scheduling/reflection behavior but must still create real packages on disk. |
| Skill loading | `loadSkills()` is at `packages/coding-agent/src/core/skills.ts:513`; project packages are discovered under `.prime/agent/skills/`. | Quarantine promotion can target the standard project skill location without a core patch. |
| Python skill provisioning | `packages/coding-agent/src/core/kernel/bootstrap.ts` installs Python skills editable and hashes `pyproject.toml`; `PRIME_AGENT_KERNEL_PYTHON` skips all installation. | The overlay Python 3.12 venv and Prime Agent's managed kernel venv remain separate. |
| Compaction | `DEFAULT_COMPACTION_SETTINGS`, `shouldCompact()`, and `prepareCompaction()` are at `compaction.ts:110`, `:206`, and `:579`. | Batch plan-preserving summarization uses `session_before_compact`; it does not fork compaction. |
| Tool truncation | `truncate.ts` defaults to 2,000 lines and 50 KiB; `output-accumulator.ts` persists full truncated output to a temporary file. | BATS observation elision must complement, not duplicate or weaken, native truncation. |
| Local models | `packages/coding-agent/docs/models.md` documents custom OpenAI-compatible providers in `~/.prime/agent/models.json`. | Phase 1 installs the shipped project template into the configured agent directory during the one-time setup step. |
| Faux provider | `packages/ai/src/providers/faux.ts`; `packages/coding-agent/test/suite/harness.ts` registers it without a real endpoint or credential. | Every Synth integration test uses deterministic faux responses or fixtures. |
| SDK | `createAgentSession()` is at `packages/coding-agent/src/core/sdk.ts:145`. | In-process tests may use the SDK; product entry points remain the `synth` extension and CLI. |

## Drift from the supplied anchors

1. The requested documentation lives under `packages/coding-agent/docs/`, not root `docs/`.
2. `ExtensionAPI` remains exactly at `extensions/types.ts:992`. Project extension discovery moved from the supplied line 552 to line 571; line 552 is now the start of `discoverAndLoadExtensions()`.
3. Public print mode is selected by `-p` or `--print` (`cli/args.ts:178`), not `--mode print`. The parser accepts `text`, `json`, `rpc`, `acp`, and `daemon` after `--mode` (`cli/args.ts:9,102-106`).
4. `packages/coding-agent/docs/daemon.md:76` says protocol v4. Current source sets `DAEMON_PROTOCOL_VERSION = 7` and `DAEMON_SCHEMA_REVISION = 22` in `daemon-protocol.ts:54,70`.
5. `packages/coding-agent/docs/settings.md:170` says transport defaults to `sse`; `SettingsManager.getTransport()` returns `auto` when unset (`settings-manager.ts:796-798`).
6. `settings.md` omits implemented settings relevant to later phases, including auto-refine. Current auto-refine defaults are enabled, every 25 assistant turns, compaction-triggered, with a 20-minute cooldown.
7. `docs/models.md` names Ollama, vLLM, and LM Studio but provides one generic OpenAI-compatible/Ollama recipe rather than separate vLLM and LM Studio recipes.
8. `ModelRegistry.create()` reads a single configured/global `models.json` path (`model-registry.ts:468`); project `.prime/agent/models.json` is not auto-loaded.
9. `session_start` has no task prompt. The first task string is available in `input` and `before_agent_start`, so task-keyed memory retrieval cannot honestly happen in `session_start`.
10. Provider hooks expose request payload and response status/headers, not native `Usage`. Assistant message events carry usage. Exact transcript entry IDs are also not present on tool/provider hooks.
11. The managed Prime Agent kernel currently pins Python 3.11. The Synth overlay requires a separate uv-managed Python 3.12 environment.
12. Native HarnessState skill entries are descriptions and call contracts, not installable skill packages.
13. The Python HarnessState store reloads on external mtime changes but writes without cross-process locking or atomic rename. Compiler work must remain serialized with refinement work.
14. The faux provider invokes the provider response callback but does not invoke the payload callback. Faux provider timing must use `context` as the request-start signal and finalized assistant events as completion.
15. `docs/compaction.md` describes `CompactionEntry.timestamp` as a number; `SessionEntryBase` uses an ISO string. `docs/session-format.md` matches source.
16. The short `ExtensionContext.hasUI` source comment says RPC is false, while the detailed extension documentation describes RPC UI-protocol support. Mode-specific tests, not that comment, decide Synth command behavior.

## Constraints carried into implementation

- First-turn memory retrieval runs after the prompt is known and before the first provider request. Session startup may initialize or restore the bridge, but it cannot perform task-keyed retrieval.
- Provider timing starts at `context` for all providers, with `before_provider_request` refining the real-provider payload boundary when available. Status and headers arrive at `after_provider_response`; token/cost usage and stream completion are finalized from the assistant message at `turn_end` or `message_end`.
- Telemetry entry pointers are correlated after transcript append by scanning the current branch at `turn_end`; hooks that lack entry IDs must not invent one.
- Print exit status is captured by the `synth` CLI child-process driver because no extension event carries it.
- Configuring the existing daemon summarizer is worker-local and does not require a wire change. Any later daemon command, event, or response change must follow the protocol rules in `AGENTS.md`.
- The shipped local model template is installed or merged by `scripts/setup-offline.sh`; no unlisted `ModelRegistry` patch is planned.
- The overlay venv must not be supplied as `PRIME_AGENT_KERNEL_PYTHON`, because that would disable automatic provisioning of promoted Python skills.
- ReasoningBank items, native HarnessState entries, packaged project skills, optimizer history, and telemetry remain distinct stores with explicit ownership.

## `/synth` command mode contract

The `/synth` command family must remain usable in interactive, print, and RPC modes; interactive rendering may be richer, but `hasUI` is never a prerequisite for the command's core result. Phase 2 tests enter `/synth status` in an 80×24 tmux TUI and assert the rendered status, run `--print "/synth status"` and assert deterministic stdout plus exit 0, and drive RPC by asserting `get_commands` advertises `synth`, sending `/synth status` through `prompt`, and checking the returned response/UI-protocol events. The same three-mode contract applies to memory and budget subcommands as they land, and later mutating commands must expose explicit headless arguments instead of relying on an interactive confirmation dialog.

## Phase 0 validation

- `npm install`: completed with npm `11.12.1`; 353 packages were added and 366 audited. npm reported one moderate and two high dependency advisories for later triage.
- `npm run build`: completed for `@prime-tui/core`, `@mariozechner/pi-ai`, `@mariozechner/pi-agent-core`, and `@mariozechner/pi-coding-agent`. The current `@mariozechner/pi-ai` build contacted remote model catalogs and rewrote `models.generated.ts`; the generated diff was removed because the catalog is pinned by the upstream baseline until permitted patch A3 in Phase 1.
- `npm run check`: Biome checked 938 files with no fixes or warnings; TypeScript checking, installer rendering, and browser smoke checks completed successfully.
- `npx tsx ../../node_modules/vitest/dist/cli.js --run test/suite`, from `packages/coding-agent`: the final post-review run passed 77 test files and 906 tests in 95.52 seconds. A reviewer run under parallel audit load had timed out in regressions 4685 and 4603; isolated 4685 then passed, isolated 4603 passed 4/4, and the final full rerun was green.
- `scripts/proof-faux-print.sh`: the bounded, isolated faux print proof uses a private agent directory and short private daemon socket, requires exact stdout `upgrade response 1`, rejects stderr, and exits nonzero unless exact-socket daemon shutdown is proven.
- `scripts/proof-faux-tui.sh`: the bounded 80×24 tmux proof uses the same isolation, asserts version/model/cwd readiness without login or provider errors, exits through `/quit` with a zero sentinel, and terminates only its exactly named tmux session and daemon socket.
- `scripts/fetch-refs.sh`: verified all three ignored, shallow, clean reference snapshots against their pinned origins and commits and completed successfully.
