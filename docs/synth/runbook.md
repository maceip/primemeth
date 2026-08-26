# Synth runbook

This runbook covers the supported local path from setup through memory, batch execution, optimization, and compiled-skill lifecycle management.

## 1. Prepare the checkout

Use Node and npm versions supported by the repository, plus `uv`, `git`, `rg`, `fd`, and `tmux`. From the repository root:

```bash
scripts/setup-offline.sh
```

The setup step installs a uv-managed Python 3.12 overlay environment, prepares the Prime Agent kernel environment, installs enabled Python-backed project skills, prefetches the configured FastEmbed model, and merges the shipped `local` model profile into the Prime Agent model catalog. It is idempotent and does not overwrite conflicting local model settings.

The default endpoint is `http://localhost:8000/v1`, with model IDs `local-main` and `local-small`. Start an OpenAI-compatible Ollama, vLLM, LM Studio, or equivalent server there, or change both `synth.json` and the local model catalog to match your endpoint. The faux-provider acceptance path needs no inference server or API key.

## 2. Inspect configuration

The checked-in `synth.json` is the local development profile. The complete generated reference is [config.md](config.md). Validate documentation drift after changing the schema:

```bash
node_modules/.bin/tsx scripts/generate-synth-config-doc.ts --check
```

An absent `synth.json` turns Synth features off. An unavailable bridge, embedder, database, or endpoint must degrade the associated feature without preventing the underlying agent session from running.

## 3. Start a session

Start the TUI with the shipped local model defaults:

```bash
./prime-agent.sh --offline
```

Inside the TUI, inspect the extension:

```text
/synth status
/synth budget
/synth memory list
```

`/synth status` reports configuration state, bridge PID and protocol, ReasoningBank state, current budget and regime, verifier mode, and skill-compiler admission state. A missing local endpoint affects model requests, not TUI boot or status inspection.

For a credential-free system proof from a clean worktree:

```bash
scripts/synth-demo.sh
```

The demo builds the pinned catalog, performs real TUI and print-mode boot proofs, runs each research loop with deterministic faux fixtures, creates a gated AutoSaddler commit on a new `synth/optimizer/*` branch without switching the source worktree, exercises all four bounded failure injections, and verifies compiler crash recovery.

## 4. Read telemetry

Each session writes `.synth/telemetry/<session-id>.jsonl`. The native Prime Agent session JSONL remains the source transcript; telemetry events carry pointers to its file and entry IDs.

Validate a stream against the mirrored Python schema:

```bash
uv run --project . --no-sync synth telemetry validate \
  .synth/telemetry/<session-id>.jsonl
```

Useful event kinds include `session.start`, `turn`, `tool`, `provider`, `budget`, `verification`, and `bridge`. Tool events contain the tool name, argument digest, duration, error class, and current budget snapshot without duplicating raw sensitive arguments.

## 5. Manage ReasoningBank memory

Set `memory.enabled` to `true` only after `scripts/setup-offline.sh` has prepared the pinned embedding model. The first bridge request opens `.synth/synth.db` in WAL mode.

List or search memories:

```bash
uv run --project . --no-sync synth memory list --limit 20
uv run --project . --no-sync synth memory search -k 3 --min-score 0.35 \
  "repair a stale cache invalidation bug"
```

Judge and distill a persisted v3 session explicitly:

```bash
uv run --project . --no-sync synth judge \
  ~/.prime/agent/sessions/<session-id>.jsonl
uv run --project . --no-sync synth distill \
  ~/.prime/agent/sessions/<session-id>.jsonl
```

`distill` also accepts a session directory for batch processing. Normal shutdown schedules distillation as a detached job and never blocks agent exit. The judge uses native task state and exit code only as priors; its local-model trajectory judgment determines success or failure extraction.

Retrieved strategies appear under `## Lessons from past sessions`. Contrastive meta-heuristics appear separately under `## Problem framing`. ReasoningBank does not write into native HarnessState.

## 6. Operate BATS budgets and verification

Positive budget values are hard ceilings. Zero means unlimited. The transcript receives the exact `<budget>` block at the initial task and after every tool result, plus the current HIGH, MEDIUM, LOW, or CRITICAL regime. Native usage provides tokens, cache use, and cost.

Inspect live state with `/synth budget`. When a class is exhausted, Synth denies the call with `No <class> budget left.` Full exhaustion adds a wrap-up instruction; unused budget plus repeated no-tool answers triggers the configured continuation nudge.

Interactive verification is off by default. Batch verification is on by default:

```bash
uv run --project . --no-sync synth run \
  --task "Fix the seeded bug and run its acceptance test" \
  --bats \
  --max-trajectories 2
```

Batch artifacts live under `.synth/batch-runs/<run-id>/`, including per-trajectory candidates, verifier JSON, patch artifacts, and `selection.json`. A `PIVOT` or `CONTINUE` remains in the current trajectory up to `verifier.maxCycles`; verified success with sufficient remaining budget can start a fresh worktree trajectory.

## 7. Run AutoSaddler

AutoSaddler requires a clean source worktree and never switches it. The default deterministic smoke uses the stub Prime Agent transport; a real local transport remains configurable through the scenario adapter.

```bash
git status --short
uv run --project . --no-sync synth optimize \
  --config optimizer/configs/synth_local.yaml \
  --run-id local-smoke-001 \
  --branch synth/optimizer/local-smoke-001
```

The command prints the selected candidate, mean development score, fix rate, regression rate, gated commit, and report path. Inspect before promoting anything:

```bash
git show --stat synth/optimizer/local-smoke-001
cat .synth/optimizer-runs/local-smoke-001/optimizer-report.json
```

Accepted output is a commit whose parent is the source revision. Rejected or non-improving runs leave the optimizer branch at the base revision. The run store also contains the EvoDAG projection, bounded optimizer history, traces, matched-batch evidence, D_dev evaluations, and deferred reflection lessons. Synth does not merge or push optimizer branches automatically.

## 8. Compile and manage skills

There are three admission paths:

- The model calls `compile_skill` after identifying a repeated, stable multi-step workflow.
- The user runs `/synth compile <workflow>` in a live session.
- The operator mines a persisted session with `synth compile --from`.

Offline compilation example:

```bash
uv run --project . --no-sync synth compile \
  --from ~/.prime/agent/sessions/<session-id>.jsonl \
  --name-hint normalize-test-output \
  --reason "The verified normalization sequence recurred across sessions"
```

Generation first lands under `.synth/quarantine/<name>/`. Promotion requires a complete Python-backed skill package, static safety approval, Ruff, and meaningful pytest cases in an ephemeral uv environment. The gate rejects network/process behavior outside its declared policy, unsafe writes, bundled-skill shadowing, recursive skill compilation, and refinement invocation.

Inspect `.synth/audit/skills.jsonl` for the actor, reason, hash, and every gate receipt. A promoted skill appears at `.prime/agent/skills/<name>/` and is loaded by the next session. In a current TUI session:

```text
/synth reload-skills
```

Demote a compiler-managed skill into recoverable storage:

```bash
uv run --project . --no-sync synth skill demote normalize-test-output \
  --reason "superseded by a narrower workflow"
```

The global `killSwitch.all` and `skillCompiler.enabled` settings stop new runtime compilation. `skillCompiler.maxPerSession` bounds admitted jobs even when multiple trigger paths are used.

## 9. Failure response

| Symptom | Expected state | Operator action |
| --- | --- | --- |
| `memory_busy` | Another process holds the SQLite write lock; the error is retryable and the DB remains intact | Retry after the competing transaction ends; inspect long-running local processes before changing timeout policy |
| One `bridge exited unexpectedly` warning | Agent continues with bridge-backed features disabled for that session | Inspect the Python 3.12 overlay environment, then start a new session; a warning storm is a bug |
| `VerifierUnavailable` telemetry | The agent’s final answer remains; no verifier decision is fabricated | Restore the configured local endpoint and rerun verification or the batch task |
| `No <tool> budget left.` | The denied call and CRITICAL/exhausted budget are persisted | Raise the next session’s configured budget only if the acceptance task justifies it; do not edit recovered telemetry |
| `synth.skill.compile` ends `failed` after restart | A detached compiler died or emitted no valid JSON receipt; the terminal failure is durable | Inspect `.synth/compiler-jobs/<request-id>.stderr` and `.synth/audit/skills.jsonl`; submit a new bounded request after correcting the cause |
| Optimizer rejects a dirty tree | Source isolation is intact and no branch commit was created | Commit or otherwise resolve only your own work, then rerun; never use reset, clean, or stash to bypass this gate |

## 10. Acceptance and maintenance

Before pushing `synth/dev`, run the repository gates required by `AGENTS.md`, the full harness regressions, wiring, host classification, faux print and TUI boot proofs, and `scripts/synth-demo.sh` from a clean exact commit. The demo creates local optimizer evidence and a local optimizer branch; both are deliberate receipts.

Do not commit `.synth/`, databases, model weights, telemetry, optimizer run stores, quarantine output, or `refs/`. Do not tag a release until the independent acceptance reviewer returns an explicit tag instruction.
