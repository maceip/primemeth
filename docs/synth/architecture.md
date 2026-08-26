# Synth architecture

Synth is a local-first coding-agent harness built as a bounded fork of Prime Agent. Four research systems share one runtime spine instead of operating as independent scripts: ReasoningBank supplies epistemic memory, BATS controls budget-aware execution and verification, AutoSaddler improves the harness offline, and the Dynamic Skill Compiler promotes recurring workflows through a deterministic quarantine gate.

## System boundary

```text
                               CONTROL PLANE — offline only
                  ┌────────────────────────────────────────────┐
                  │ AutoSaddler v2 + synth_harness scenario    │
                  │ trace → diagnose → patch → batch verify    │
                  │ → D_dev gate → synth/optimizer/* commit    │
                  └───────────────────┬────────────────────────┘
                                      │ reads traces, commits gated patches
                                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│ Prime Agent runtime                                                      │
│                                                                           │
│  .prime/agent/extensions/synth/index.ts       synth CLI / synth-bridge    │
│  ┌──────────────────────────────────────┐     ┌────────────────────────┐  │
│  │ session hooks and commands           │ JSON│ embeddings and SQLite │  │
│  │ telemetry, budgets, verifier         ├────►│ judge and distillation│  │
│  │ memory injection, compiler trigger   │lines│ batch and optimizer   │  │
│  │ tool-call data-plane guard           │◄────┤ compiler and gate     │  │
│  └──────────────────┬───────────────────┘     └────────────┬───────────┘  │
│                     │                                      │              │
│                     ▼                                      ▼              │
│  native v3 transcript + .synth/telemetry       .synth/synth.db           │
│  native HarnessState                           .synth/audit and quarantine│
│                                                .prime/agent/skills        │
└───────────────────────────────────────────────────────────────────────────┘
                                      ▲
                                      │ promoted only after static, lint,
                                      │ and ephemeral-environment tests
                  ┌───────────────────┴────────────────────────┐
                  │ Dynamic Skill Compiler — runtime data plane│
                  └────────────────────────────────────────────┘
```

There are exactly two product entry points:

- The TypeScript extension at `.prime/agent/extensions/synth/index.ts` owns all behavior synchronous with a Prime Agent session.
- The Python `synth` CLI, including its `synth-bridge` entry, owns embeddings, persistence, judging, batch orchestration, optimization, and skill gating.

`scripts/check-wiring.sh` walks both import graphs and rejects an overlay source file that is not transitively reachable from one of those roots. Demo and documentation generators are classified as explicit operator drivers rather than runtime roots.

## Runtime sequence

1. At `session_start`, the extension reads `synth.json`. An absent file means Synth is off and Prime Agent continues normally. A valid file initializes telemetry and crash-recovers budget state from that stream.
2. If ReasoningBank is enabled, the extension starts one lazy JSON-lines bridge and requests instruction-aware retrieval. Strategy items render under `## Lessons from past sessions`; contrastive meta-heuristics render separately under `## Problem framing`.
3. Before model execution, BATS contributes the regime playbook and persistent plan discipline. The context hook carries the initial and current `<budget>` blocks, applies old-observation elision, and preserves native transcript entries as the source of truth.
4. Every tool call passes through the data-plane write guard and the per-tool budget gate. Every tool result receives updated budget state. Native provider usage supplies token, cache, and cost accounting; Synth has no second tokenizer.
5. When an agent concludes, the strategic verifier can return `SUCCESS`, `CONTINUE`, or `PIVOT`. Interactive verification is opt-in; Synth batch mode enables it by default and persists the decision before following guidance.
6. At shutdown, ReasoningBank distillation is detached and non-blocking. Its Python prompt path deterministically condenses long tool observations while retaining head, tail, process exits, stack frames, and terminal exceptions, then applies the configured conservative trajectory ceiling. Native task state and process exit status are judge priors, never the sole correctness signal.

All session telemetry points back to the native v3 transcript by session file and entry identifier. Telemetry is an append-only operational index, not a replacement transcript.

## Research-system integration

| System | In-loop contribution | Durable state | Offline or batch contribution |
| --- | --- | --- | --- |
| ReasoningBank | Retrieves success and failure strategies at session start | `memory_items` and sqlite-vec rows in `.synth/synth.db` | Judges and distills up to three generalizable items per trajectory; contrast-distills problem-framing meta-heuristics |
| BATS | Injects budgets, regimes, plan discipline, observation elision, enforcement, continuation nudges, and strategic verification | Budget snapshots and verifier decisions in telemetry and the native session | Creates fresh worktree trajectories, records verified candidates, and selects the best candidate |
| AutoSaddler v2 | None; it never mutates the live runtime | Content-addressed run store under `.synth/optimizer-runs` | Samples mini-batches, diagnoses and patches, validates the matched batch and D_dev, then creates a durable commit on `synth/optimizer/*` |
| Dynamic Skill Compiler | Schedules compilation through `compile_skill`, `/synth compile`, or native refinement serialization | Quarantine, audit records, compiler-job receipts, promoted project skills | Mines a session, generates a complete Python skill package, and applies deterministic safety, Ruff, and ephemeral-venv pytest gates |

The fidelity ledger is `DECISIONS.md`. Prompt integrity hashes and source provenance are available through `synth ports`.

## Data plane and control plane

Runtime self-modification is limited to:

- `.prime/agent/skills/`
- native HarnessState JSON
- `.synth/`, including SQLite, telemetry, audit, quarantine, demotion, candidates, and compiler jobs

The extension denies tool writes outside that set and records the denial. The skill gate repeats the boundary statically, prohibits recursive compilation or refinement, rejects unsafe process/network behavior, prevents bundled-skill shadowing, and requires meaningful tests before promotion.

The control plane includes `packages/`, `prime-agent-runtime/`, `.prime/agent/extensions/`, and the overlay Python packages. It changes only through normal development or an AutoSaddler run on its own branch. The `synth_harness` scenario restricts candidate paths to project skills and overlay prompt/config assets, rejects evaluation identifiers in patches, snapshots and rolls back failed candidates, runs the matched batch, and gates accepted candidates on the disjoint development set.

Synth-managed batch and optimizer Git worktrees recursively clear all write bits from `packages/`, `prime-agent-runtime/`, and `.prime/agent/extensions/` immediately after creation. Exact original modes are restored before teardown, and spawned sessions carry a managed-workspace marker. Before reading `synth.json`, the extension scans every protected descendant and warns once if any remains writable or unsafe. This is defense in depth, not full OS isolation: a determined process running as the same user can restore modes or replace protected roots through writable parents, and root can bypass them. This host does not provide a Synth container or Firecracker boundary.

## Storage and concurrency

- `.synth/synth.db` uses WAL mode and a busy timeout. Memory commands report a retryable `memory_busy` error under sustained lock contention.
- `.synth/telemetry/<session-id>.jsonl` has one TypeBox-validated event per session, turn, tool, provider exchange, budget transition, verifier decision, or bridge action. The extension is its single writer.
- `.synth/compiler-jobs/<request-id>.{json,stderr}` plus `synth.skill.compile` transcript entries make detached skill compilation restartable. Terminal receipts are force-flushed even before the first assistant message.
- `.synth/audit/skills.jsonl` records compiler actor, reason, content hash, every deterministic gate result, promotion, and demotion.
- Native Prime Agent v3 JSONL sessions remain authoritative for conversation state, task outcome, tree structure, and transcript replay.

SQLite WAL permits concurrent Synth sessions. Promotion and demotion use a dedicated skill lock so two compilers cannot publish the same package concurrently.

## Endpoint and failure behavior

Runtime inference targets only the configured OpenAI-compatible endpoint. The shipped model profile points at localhost; no Prime-hosted model is preferred in the default path. Optional remote-backed integrations remain installed only where upstream requires them and stay disabled until explicitly configured.

| Failure | Required behavior |
| --- | --- |
| Bridge process exits | Emit one bounded warning, mark bridge features unavailable, and preserve the agent session |
| SQLite remains locked | Return retryable `memory_busy`; do not corrupt or replace the database |
| Local endpoint disappears during verification | Preserve the completed agent answer, omit fabricated verifier state, and record one `VerifierUnavailable` event |
| Budget reaches zero during tool use | Persist the denied call, block further calls in that class, and inject an honest wrap-up instruction |
| Detached compiler dies before an outcome | Reconcile the durable `started` job on resume and force-flush a terminal `failed` receipt |
| Invalid or absent `synth.json` | Warn once for invalid input or stay silent when absent, then run vanilla Prime Agent |

See [config.md](config.md) for the generated schema reference and [runbook.md](runbook.md) for operator procedures.
