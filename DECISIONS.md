# Synth Decisions

Append-only journal. Each entry records a deviation, adaptation, or boundary decision that affects the implementation.

## 2026-08-25 — D-0001: Use package-local documentation paths

- Decision: Treat `packages/coding-agent/docs/` as the authoritative location for the requested Prime Agent documentation.
- Alternatives: Create root-level copies or symlinks; rely on the supplied root `docs/*` paths.
- Why: Root `docs/` does not exist at upstream `9bc005574`; copying documentation would create stale duplicates.
- Affected files: `ARCHITECTURE_NOTES.md`, `INTEGRATION_MAP.md`.

## 2026-08-25 — D-0002: Use the actual public print flag

- Decision: Gate and demo commands use `--print` or `-p`; reports may describe this as print mode.
- Alternatives: Use the requested literal `--mode print`; patch the CLI to add an alias.
- Why: The current parser does not accept `print` after `--mode`. A CLI compatibility patch is unnecessary and is not in Appendix A.
- Affected files: Phase gate commands, future `scripts/synth-demo.sh`, `ARCHITECTURE_NOTES.md`.

## 2026-08-25 — D-0003: Retrieve task-keyed memory at first prompt, not session_start

- Decision: `session_start` initializes bridge/session state; the extension performs task-keyed ReasoningBank retrieval in `before_agent_start`, before the first provider request, and injects the result there.
- Alternatives: Query with an empty task during `session_start`; add a core hook payload; retrieve after the first provider call.
- Why: `SessionStartEvent` contains only lifecycle reason and previous session file. `BeforeAgentStartEvent` is the first hook carrying the expanded task prompt and can still modify the system prompt. This preserves the paper's first-call retrieval behavior without a core patch.
- Affected files: Future `.prime/agent/extensions/synth/` memory integration; Appendix B ReasoningBank item adapted only in hook timing.

## 2026-08-25 — D-0004: Install the local model template during setup

- Decision: Ship a project `.prime/agent/models.json` template and have `scripts/setup-offline.sh` safely install or merge its `local` provider into the configured agent-directory `models.json`.
- Alternatives: Patch `ModelRegistry` to auto-load project models; wait for the Phase 2 extension to register the provider; overwrite an existing global model catalog.
- Why: `ModelRegistry.create()` reads one configured/global path. A core patch is outside Appendix A, extension registration is too late for Gate 1, and overwriting user configuration is unsafe.
- Affected files: Future `.prime/agent/models.json`, `scripts/setup-offline.sh`, Phase 1 documentation.

## 2026-08-25 — D-0005: Complete provider telemetry on finalized messages

- Decision: Treat `context` as the universal request-start event, enrich real HTTP exchanges with `before_provider_request` and `after_provider_response`, and close each exchange with native Usage at `message_end` or `turn_end`.
- Alternatives: Depend only on the two provider hooks; estimate usage; modify the faux provider.
- Why: Provider hooks do not carry Usage, `after_provider_response` precedes stream consumption, and the faux provider does not invoke the payload hook. Final assistant messages already contain authoritative native Usage.
- Affected files: Future Synth extension telemetry implementation and Phase 2 faux integration tests.

## 2026-08-25 — D-0006: Persist budget blocks through tool_result

- Decision: Use `tool_result` to append the BATS `<budget>` block to finalized tool results, and also use `context` to ensure request-time presence and perform old-observation elision.
- Alternatives: Rewrite only the request-local context; add a core transcript patch.
- Why: `context` mutations are not persisted to the native transcript. The existing `tool_result` hook provides the same in-loop placement and satisfies transcript-level Gate 3 evidence without a vendored-tree change.
- Affected files: Future Synth extension BATS integration; Appendix B hook adaptation with prompt text unchanged.

## 2026-08-25 — D-0007: Pin reference snapshots in the fetch script

- Decision: `scripts/fetch-refs.sh` fetches and verifies the exact Phase 0 commits, origin URLs, shallow state, and clean worktrees. Updating a paper snapshot requires an explicit script and architecture-note change.
- Alternatives: Shallow-clone each moving default branch; record hashes only after cloning; accept any existing Git directory.
- Why: Fidelity work must be reproducible across fresh clones, and an invalid or substituted reference checkout must fail closed rather than print a misleading receipt.
- Affected files: `scripts/fetch-refs.sh`, `ARCHITECTURE_NOTES.md`.

## 2026-08-25 — D-0008: Separate the overlay Python from the managed kernel

- Decision: Overlay package metadata requires Python `>=3.12,<3.15`, with Python 3.12 selected by uv. Prime Agent continues to provision its own Python 3.11 kernel environment.
- Alternatives: Use the host Python 3.14; reuse the managed kernel; set `PRIME_AGENT_KERNEL_PYTHON` to the overlay venv.
- Why: The overlay dependency wheels and AutoSaddler support Python 3.12, while supplying the overlay interpreter as the kernel interpreter would skip automatic promoted-skill installation.
- Affected files: `pyproject.toml`, `README.md`, `AGENTS.md`, future `scripts/setup-offline.sh`.

## 2026-08-25 — D-0009: Keep generated state out of the source scaffold

- Decision: The removed `local_memory/reasoning_bank.db` is obsolete. Runtime SQLite files, WAL sidecars, telemetry, quarantine state, and repository-local model artifacts live under ignored `.synth/`; model caches may also live outside the repository.
- Alternatives: Keep a source-adjacent database; commit a seeded database; scatter generated state beside Python modules.
- Why: The database is runtime data, not source. One ignored state root prevents accidental commits and gives the bridge, compiler, and optimizer a shared ownership boundary.
- Affected files: `.gitignore`, `README.md`, `local_memory/schema.sql`, future setup and storage code.

## 2026-08-25 — D-0010: Treat core patches and promoted skills as subordinate runtime surfaces

- Decision: The `synth` extension and CLI remain the two product roots. Appendix A patches execute inside the existing Prime Agent path, and promoted skill packages are data-plane artifacts loaded by the existing skill/kernel runtime. The wiring checker classifies both explicitly.
- Alternatives: Count each patched core file or promoted package as another product entry point; hide them from wiring checks; wrap the Prime Agent runtime with a third launcher.
- Why: The permitted patches and standard skill loader are execution mechanisms below the two user-facing roots, but omitting them from the graph would make the orphan invariant unverifiable.
- Affected files: `INTEGRATION_MAP.md`, future `scripts/check-wiring.sh`, Appendix A patches, `.prime/agent/skills/`.

## 2026-08-25 — D-0011: Correlate transcript identity after persistence

- Decision: Tool/provider telemetry records lifecycle identifiers immediately, then resolves stable native transcript entry pointers at `turn_end`. The external `synth` CLI records print-process exit status for judge and batch inputs.
- Alternatives: Invent entry IDs in hooks that do not expose them; copy the transcript into telemetry; patch core events to carry entry IDs and process exit status.
- Why: Current hooks expose neither final transcript IDs at execution time nor the print exit code. Post-append correlation preserves the native transcript as source of truth without an unlisted core patch.
- Affected files: Future Synth extension telemetry, Python judge/batch runner, Phase 2 and Phase 4 tests.

## 2026-08-25 — D-0012: Serialize compiler and HarnessState mutations

- Decision: Skill compilation/promotion is scheduled so it cannot race with native refinement or Python HarnessState mutations in the same session.
- Alternatives: Allow independent concurrent writers; add an unlisted Prime Agent runtime patch; merge ReasoningBank and compiler state into HarnessState.
- Why: TypeScript HarnessState writes atomically, but the Python runtime reloads by mtime and writes directly without cross-process locking or atomic rename. Serialization avoids lost updates while preserving store separation.
- Affected files: Future Synth extension compiler scheduling, bridge operations, Phase 7 concurrency tests.

## 2026-08-25 — D-0013: Retain the inherited tokenizer dependency only until Phase 3

- Decision: The uncommitted overlay baseline retains `tiktoken` in Phase 0 but no Synth code may import or depend on it. Phase 3 removes it when BATS adopts native `Usage` accounting.
- Alternatives: Remove it during reconnaissance; use it for overlay token estimates.
- Why: The phase plan explicitly schedules removal in Phase 3. Retaining the scaffold dependency temporarily preserves phase ordering without weakening the no-tokenizer architecture.
- Affected files: `pyproject.toml`, future Phase 3 dependency update.
