<img width="512" src="https://github.com/user-attachments/assets/dbeb27d8-e24d-4a5e-a7a4-99fe2f8cc658" />

# PRIME METG

Prime Meth is a local-first, self-hostable coding-agent harness built on a vendored [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) workspace. It combines ReasoningBank memory, BATS budget-aware execution and strategic verification, an AutoSaddler v2 outer optimization loop, and a quarantine-gated Dynamic Skill Compiler behind one TypeScript extension and one Python CLI.

The vendored baseline is upstream commit `9bc005574`. Synth development lives on `synth/dev`; runtime and optimizer state are local and ignored.

## What Synth adds

- ReasoningBank distills generalizable lessons from successful and failed v3 session trajectories, stores them in local SQLite plus sqlite-vec, and retrieves them into the next related session.
- BATS exposes per-tool, token, and cost budgets in the transcript, changes execution regimes as resources fall, denies exhausted calls, elides stale observations, and verifies conclusions as `SUCCESS`, `CONTINUE`, or `PIVOT`.
- Batch scaling runs fresh worktree trajectories, records verified candidates, and selects the best result with pass, select, and first-attempt metrics.
- AutoSaddler v2 optimizes the harness through matched training mini-batches and a disjoint development gate, creating durable accepted commits only on `synth/optimizer/*` branches.
- The Dynamic Skill Compiler turns recurring workflows into complete Python-backed skills, tests them in ephemeral uv environments, applies deterministic static safety gates, and promotes only accepted packages into `.prime/agent/skills/`.
- Append-only Synth telemetry indexes the native Prime Agent transcript without replacing it. All runtime model traffic targets the configured OpenAI-compatible endpoint.

## Quick start

Prerequisites are Node/npm supported by the repository, `uv`, `git`, `rg`, `fd`, and `tmux`.

```bash
scripts/setup-offline.sh
./prime-agent.sh --offline
```

The setup script prepares a uv-managed Python 3.12 overlay environment, the separate Prime Agent kernel environment, enabled Python-backed project skills, the pinned FastEmbed model, and a local model-catalog entry. It is idempotent and preserves conflicting user model settings.

The checked-in profile points `local-main` and `local-small` at `http://localhost:8000/v1`. Start an OpenAI-compatible local server there or update `synth.json` and the installed Prime Agent model catalog together. `SYNTH_LOCAL_API_KEY` is only a local configuration reference; servers that ignore authentication may use any nonempty value.

Inside the TUI:

```text
/synth status
/synth budget
/synth memory list
```

The Python control surface is available through the overlay environment:

```bash
uv run --project . --no-sync synth status
uv run --project . --no-sync synth memory search "similar repair task"
uv run --project . --no-sync synth run --task "fix the seeded bug" --bats
```

## Credential-free acceptance demo

From a clean exact commit:

```bash
scripts/synth-demo.sh
```

The demo uses the faux provider to prove the complete loop: one task fails, its lesson is distilled and retrieved by the next session, BATS budgets and enforcement appear in the persisted transcript, strategic verification pivots a failing attempt, AutoSaddler creates a regression-free gated commit on its own optimizer branch, and the compiler generates, rejects or promotes, reloads, and invokes a tested skill. It also performs real TUI and print-mode boot proofs and the bounded Phase 8 failure injections.

The demo never switches or modifies the source worktree, but it deliberately leaves ignored run evidence under `.synth/optimizer-runs/` and a local `synth/optimizer/*` commit as acceptance receipts.

## Architecture and operations

- [Architecture](docs/synth/architecture.md)
- [Generated configuration reference](docs/synth/config.md)
- [Operator runbook](docs/synth/runbook.md)
- [Verified Prime Agent anchors](ARCHITECTURE_NOTES.md)
- [Capability-to-hook integration map](INTEGRATION_MAP.md)
- [Append-only implementation decisions](DECISIONS.md)

The two runtime heads are `.prime/agent/extensions/synth/index.ts` and the `synth` CLI entry in `synth/cli.py`. `scripts/check-wiring.sh` rejects unreachable overlay TypeScript or Python modules.

Runtime self-modification is confined to `.prime/agent/skills/`, native HarnessState, Synth SQLite state, and `.synth/`. The extension’s tool guard and the compiler’s static gate both enforce that boundary. Prime Agent packages, `prime-agent-runtime/`, the Synth extension, and overlay Python remain control-plane code and change only through normal development or a gated offline optimizer branch.

## Repository layout

```text
.prime/agent/extensions/synth/   TypeScript runtime extension
.prime/agent/skills/             trusted project skills and compiler promotions
synth/                           Python CLI and JSON-lines bridge service
local_memory/                    ReasoningBank storage, retrieval, judge, distillation
middleware/                      telemetry schema and BATS budget math
optimizer/                       AutoSaddler v2 vendor plus Synth scenario adapter
compiler/                        skill generation, audit, promotion, and deterministic gate
tests/harness_regression/        cross-phase deterministic regression suite
tests/harness_tasks/             disjoint optimizer training and development fixtures
scripts/synth-demo.sh            full-loop acceptance proof
```

Runtime databases, telemetry, candidates, quarantine, audit logs, and optimizer runs belong under ignored `.synth/`. Research snapshots belong under ignored `refs/`. Never commit those paths, model weights, or native session data.

Overlay Python commands must use the uv-managed environment; do not use the host Python. Fetch the pinned research snapshots for fidelity audit with:

```bash
scripts/fetch-refs.sh
```

Vendored Prime Agent documentation remains under `packages/coding-agent/docs/`.
