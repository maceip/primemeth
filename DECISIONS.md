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

## 2026-08-25 — D-0014: Merge the local model catalog without replacing user configuration

- Decision: `scripts/setup-offline.sh` resolves the destination exactly as the runtime does: `${PRIME_AGENT_CODING_AGENT_DIR}` after `~` expansion when that nonempty variable is set, otherwise `$HOME/.prime/agent`, followed by `models.json`. The tracked `.prime/agent/models.json` is the source template. Setup parses both files with the runtime's JSONC rules (`//` comments and trailing commas), validates the root object, `providers` object, provider objects, and model arrays, and refuses malformed input, a symlink, or a non-regular destination without writing. If the destination is absent, setup creates the full template. If it exists, every unrelated provider and unknown field is retained; an absent template provider is added whole; for an existing template provider, only absent provider keys and absent model IDs are added. Existing values and existing model objects always win, including conflicts, and no array entry is reordered or removed. Each addition and preserved conflict is printed with its provider/key/model identity; a semantic no-op prints `models.json: no changes` and leaves the original bytes and mtime untouched. A changed file is written to an exclusive same-directory temporary file, flushed, atomically renamed, and directory-flushed; a new file uses mode `0600`, while replacement preserves the existing permission bits. Setup tests cover an absent destination, unrelated providers, an identical rerun, a conflicting `local` provider, appending a missing model, malformed JSONC with no mutation, non-regular targets, byte-stable no-op behavior, permissions, and atomic cleanup.
- Alternatives: Overwrite the destination; merge only at the provider level; silently replace conflicting local models; edit the file in place; load the project template through an unlisted `ModelRegistry` patch.
- Why: The one-time setup step must make Synth usable without taking ownership of a user's global model catalog. Field/model-level additive merge handles partially configured local providers, while fail-closed validation and atomic replacement prevent truncation or ambiguous recovery.
- Affected files: Future `.prime/agent/models.json`, `scripts/setup-offline.sh`, its merge helper, and `tests/harness_regression/test_setup_offline_models_merge.py`.

## 2026-08-25 — D-0015: Patch mature advisory fixes and retain one explicit residual risk

- Decision: The Phase 0 audit contained three advisories. `nanoid@3.3.17` was affected by high-severity [GHSA-2v37-7h3g-55p8 / CVE-2026-67213](https://github.com/advisories/GHSA-2v37-7h3g-55p8); update now to `3.3.18`, released 2026-08-07. `protobufjs@7.6.4` was affected by moderate-severity [GHSA-j3f2-48v5-ccww / CVE-2026-59877](https://github.com/advisories/GHSA-j3f2-48v5-ccww); update now to `7.6.5`, released 2026-07-04. Both compatible transitive fixes exceed the seven-day minimum age and are now pinned in `package-lock.json`. `extract-zip@2.0.1` remains affected by high-severity [GHSA-jmr9-qjv8-65gv / CVE-2026-56876](https://github.com/advisories/GHSA-jmr9-qjv8-65gv); defer version remediation because no patched release exists and `npm audit` reports `fixAvailable: false`. Do not classify it as globally not applicable: macOS/Linux helper downloads use tar archives and `PI_OFFLINE=1` returns before download, but the supported online Windows helper path still extracts ZIP assets. The Phase 1 local/offline defaults and setup-time `fd`/`rg` check are compensating controls; removal, replacement, or caller-side archive hardening requires a separately tested vendored-tree security decision.
- Alternatives: Ignore all three because observed exploit preconditions are narrow; bypass the release-age policy; suppress the residual audit; call `extract-zip` unreachable on the current machine.
- Why: Compatible, mature fixes should not remain pending. The unpatched extractor must remain visible until its reachable online Windows path is eliminated or hardened rather than being hidden by a macOS-only assessment.
- Affected files: `package-lock.json`, this risk journal, future dedicated `extract-zip` remediation if authorized.

## 2026-08-25 — D-0016: Distinguish load-only timeouts from red gates with a recurrence threshold

- Decision: A test timeout observed under concurrent audit/build/test load is recorded with the exact test and load context. If the same test passes twice in isolated reruns, the event is monitored but is not a red gate; neither the timeout nor the test is raised, skipped, weakened, or otherwise tuned away. A third such recurrence for that test across Synth phases escalates to a real investigation before the current gate can be declared green. The Phase 0 ledger starts with `test/suite/regressions/4685-daemon-client-modes.test.ts` and `test/suite/regressions/4603-worker-recovery.test.ts`, which timed out during a fresh-reviewer run concurrent with other audit activity; isolated rerun receipts are retained in F1 evidence.
- Alternatives: Treat every load-correlated timeout as a product failure; ignore timeouts after one passing rerun; increase test timeouts; remove parallel-sensitive assertions.
- Why: Daemon process tests are sensitive to machine scheduling, but dismissing recurring failures would conceal reliability regressions. Two clean isolated reruns establish a bounded load classification, and the cross-phase threshold forces investigation if the pattern persists.
- Affected files: Gate reports and test evidence from Phase 1 onward; no runtime or test timeout changes.

## 2026-08-25 — D-0017: Amend Gate 1 to build and demonstrate the local-first capability before minimal tests

- Decision: Phase 1 implements A1, A2, A3, A4, and A6 plus the A5 setting required by A2, the local model template, and `scripts/setup-offline.sh`. Each capability slice is wired and added to `scripts/synth-demo.sh`, then exercised in a real faux/local-config session before its smallest useful tests are written. Demo assertions count toward an approximate ceiling of eight tests per feature. Gate 1 retains one small banned-host grep regression, but defers the HTTP-interception offline e2e to Phase 8, where failure injection and concurrency hardening already belong. Gate 1 runtime evidence is a local-catalog TUI boot without a login prompt, a successful faux print session, configured summarization plus missing-config no-op, the pinned offline build, both reusable boot proofs, and `npm run check`.
- Alternatives: Build the original broad static/interceptor test harness before exercising the product; require the full faux suite at every focused commit; defer the live demo until Phase 8.
- Why: The operator's standing directive prioritizes a connected, demonstrably useful capability over early conformance scaffolding. The interceptor remains an important final offline acceptance check, but it does not move a Phase 1 runtime capability and is more valuable alongside Phase 8 network-failure injection.
- Affected files: `PHASE1_PLAN.md`, future `scripts/synth-demo.sh`, `tests/harness_regression/test_no_vendor_hosts.py`, Phase 1 gate evidence, and the deferred Phase 8 offline e2e.

## 2026-08-25 — D-0018: Bootstrap the kernel with the enabled Python-skill set

- Decision: `scripts/setup-offline.sh` invokes a repository-side bootstrap wrapper that resolves the same enabled skill resources as Prime Agent and passes their Python packages to the existing `ensureKernelPython` bootstrap API. It does not invoke the bare `bootstrap-cli.ts` entry point.
- Alternatives: Invoke the requested bare bootstrap CLI; provision only the base kernel and rely on the first runtime session to install skills; patch the vendored bootstrap CLI to discover resources.
- Why: The bare CLI calls `ensureKernelPython()` without `pythonSkills`. On an existing skill-aware environment this rewrites `.bootstrap-version` with an empty skill set, and on a fresh environment it leaves Python-backed bundled skills for a later potentially offline session. The extension API cannot provision dependencies before runtime, while a setup-side wrapper can use the existing public bootstrap and resource-loader behavior without an additional vendored-tree patch.
- Affected files: `scripts/setup-offline.sh`, `scripts/bootstrap-synth-kernel.ts`; the Phase 1 setup requirement is adapted from the bare CLI to its skill-aware core API.

## 2026-08-25 — D-0019: Keep the A1 fallback regression beside the private selector

- Decision: Update `packages/coding-agent/test/model-resolver.test.ts` with focused regressions for normal fallback order and the configured local default.
- Alternatives: Exercise only the public CLI in `scripts/synth-demo.sh`; copy model-selection logic into an overlay test; expose another core API solely for testing.
- Why: A1 is a permitted core fallback change, but the extension API cannot invoke the private startup fallback selector with controlled model snapshots. The package-local unit regression directly protects the changed behavior without duplicating it or widening the runtime API.
- Affected files: `packages/coding-agent/test/model-resolver.test.ts`.

## 2026-08-25 — D-0020: Resolve the daemon session-summary model in worker settings

- Decision: Add the optional top-level `daemonSessionSummary.provider/model` setting and its fail-closed accessor to `SettingsManager`, with package-local regression coverage.
- Alternatives: Configure the built-in summarizer from the Synth extension; add a daemon protocol field; nest the setting under a multi-level daemon object; depend on `defaultModel`.
- Why: The extension API cannot configure the worker-owned `DaemonSessionSummarizer`, set its recap state, or persist its `agent_status` updates. A top-level object also matches the settings manager's one-level nested merge, allowing global and project halves to compose without widening the daemon wire protocol. The package-local test is necessary because the extension cannot inject controlled global/project settings into this private worker accessor.
- Affected files: `packages/coding-agent/src/core/settings-manager.ts`, `packages/coding-agent/test/settings-manager.test.ts`.

## 2026-08-25 — D-0021: Prove local daemon recaps through a package-local faux provider

- Decision: Keep the A2 runtime patch inside the worker-owned summarizer, document the optional setting beside the daemon settings, and add a package-local faux extension plus focused summarizer regression for cross-process proof. The change is backward-compatible and adds no daemon command, event, response field, capability, protocol-version, or schema-revision change.
- Alternatives: Trust only a direct unit probe; call a real local inference endpoint; patch the daemon protocol or extension events to inject summary responses.
- Why: The extension API cannot replace the daemon's sweep/debounce lifecycle, persist native `agent_status` entries, or inject a provider into a separately launched worker without an extension fixture. The faux provider gives the demo deterministic local-main and local-small responses without a key or network request, while the package-local regression protects the private model-selection and bounded-failure behavior. The authoritative settings documentation must describe the new startup option.
- Affected files: `packages/coding-agent/src/modes/daemon/daemon-session-summarizer.ts`, `packages/coding-agent/test/daemon-session-summarizer.test.ts`, `packages/coding-agent/test/fixtures/synth-local-summary-extension.ts`, `packages/coding-agent/docs/settings.md`, `.prime/agent/settings.json`, `scripts/synth-demo.sh`.

## 2026-08-25 — D-0022: Make product analytics an explicit opt-in

- Decision: Change the core telemetry startup policy so unconfigured production settings are disabled, while retaining explicit settings and environment opt-ins and the absolute offline/do-not-track opt-outs. Keep the existing analytics transport available for documented opt-in deployments.
- Alternatives: Disable analytics only in the shipped Synth settings; remove the analytics client; route policy through the future Synth extension.
- Why: A shipped setting alone would leave fresh or differently scoped installations on the upstream default, while removing the client would amputate intentional self-hosted functionality. The extension loads after startup telemetry decisions and cannot reliably redefine them. The package-local regression and authoritative settings documentation must directly reflect the changed core default.
- Affected files: `packages/coding-agent/src/core/telemetry.ts`, `packages/coding-agent/test/telemetry.test.ts`, `packages/coding-agent/docs/settings.md`, `.prime/agent/settings.json`, `scripts/synth-demo.sh`.

## 2026-08-25 — D-0023: Remove the Prime vendor skill and make remote search opt-in

- Decision: Delete the bundled `prime-intellect` skill and its remote product references, keep the `websearch`, Linear, and Notion implementations intact, and disable only bundled `websearch` in shipped Synth project settings. Project or explicitly configured websearch skills remain loadable, and the Serper-backed bundle remains available through an explicit `true` setting.
- Alternatives: Retain and force-exclude the Prime skill in settings; remove all remote integration implementations; let the future Synth extension filter the resolved skill list after startup.
- Why: Appendix A A6 expressly permits the bundled-skill change, and removing the vendor-specific package prevents it from resurfacing outside this one project setting. The extension API cannot remove files from the distributed built-in skill catalog or reliably filter them before every startup/kernel provisioning path. Package-local loader tests and authoritative package documentation are necessary additional vendored-tree touches so the removed skill and changed shipped default are not contradicted by regressions or user guidance.
- Affected files: `packages/coding-agent/skills/prime-intellect/**`, `packages/coding-agent/test/builtin-skills.test.ts`, `packages/coding-agent/test/resource-loader.test.ts`, `packages/coding-agent/docs/skills.md`, `packages/coding-agent/docs/settings.md`, `packages/coding-agent/README.md`, `.prime/agent/settings.json`, `scripts/synth-demo.sh`.

## 2026-08-26 — D-0024: Make offline setup fail closed on the runtime-enabled skill set

- Decision: Resolve setup-time Python skills through `createAgentSessionServices`, including its MCP authentication exclusions, and verify that every enabled skill import succeeds after kernel bootstrap before setup may report ready. The Phase 1 demo uses the same runtime service construction for A6, proves unauthenticated Linear and Notion remain excluded without removing their implementations, and failure-injects an un-installable enabled skill. The banned-host regression scans all nonlocal URL literals once and rejects any source path that is not explicitly classified; the automatic release-manifest path remains allowed only with its earlier `PI_OFFLINE` guard.
- Alternatives: Keep the bare resource loader and duplicate MCP exclusions; change the vendored kernel bootstrap to throw on every skill install failure; trust bootstrap warnings; enumerate only known vendor-domain tokens.
- Why: Gate review showed that the bare loader over-provisioned unauthenticated integration packages and that `ensureKernelPython` intentionally tolerates individual skill-install failures for normal runtime degradation. Setup has a stricter contract than runtime, so its wrapper must reuse the runtime resource graph and add fail-closed post-install verification without changing vendored behavior. Classifying every discovered nonlocal URL path prevents an unlisted vendor host from bypassing a token-only grep while preserving explicit provider, auth, metadata, and offline-gated maintenance surfaces.
- Affected files: `scripts/bootstrap-synth-kernel.ts`, `scripts/synth-demo.sh`, `tests/harness_regression/test_no_vendor_hosts.py`.

## 2026-08-26 — D-0025: Verify current skill receipts and clarify absent-recap behavior

- Decision: Setup verifies that every runtime-enabled Python skill appears in the current kernel bootstrap receipt with its exact resolved package path, pyproject path, and content hash, then also verifies its import. Import success alone is insufficient because a standard-library or stale module can share the requested name. The demo failure fixture deliberately collides with Python's `json` module to protect this boundary. Separately, the Phase 1 phrase “missing-config no-op” is superseded: absent recap configuration performs no provider request and persists no generated recap, but the existing lifecycle may still persist an empty `needs_input` status entry.
- Alternatives: Trust imports alone; parse warning text from `ensureKernelPython`; patch the vendored bootstrap to throw instead of preserving runtime degradation; retain “no-op” as shorthand.
- Why: A setup command must not report offline readiness from a same-named module or stale editable install after the requested skill installation failed. Receipt path/hash evidence identifies the exact successful sync while the import check verifies usability. Precise recap wording distinguishes provider behavior from the unchanged session-status lifecycle and removes an ambiguity left in D-0017.
- Affected files: `scripts/bootstrap-synth-kernel.ts`, `scripts/synth-demo.sh`, `PHASE1_PLAN.md`, `INTEGRATION_MAP.md`, Phase 1 gate reports.

## 2026-08-26 — D-0026: Interpret local-first as vendor independence

- Decision: Runtime features may use any user-configured OpenAI-compatible endpoint, but may not depend on a specific vendor cloud; cancel the planned zero-nonlocalhost HTTP interceptor, retain the vendor-host classifier, and keep `PI_OFFLINE` only as an available proof mode.
- Alternatives: Require air-gapped operation after setup; restrict all configured endpoints to loopback.
- Why: The operator clarified Law 3 as replacement of vendor services with local or self-hostable equivalents, not a ban on ordinary user-directed networking. This also records the Phase 1 test trim required by standing directive v4.
- Affected files: `DECISIONS.md`, `INTEGRATION_MAP.md`, `PHASE1_PLAN.md`, Phase 1 report, and the canceled Phase 8 interceptor work item.

## 2026-08-26 — D-0027: Never synchronize the overlay environment at runtime

- Decision: The extension launches `uv run --project <cwd> --no-sync synth-bridge`; a missing prepared environment disables the bridge once for that session. Phase 2 exposes and validates the `memory.query` and `memory.insert` wire contracts but returns `feature_disabled` until the Phase 4 fidelity sources are reread and the store is implemented.
- Alternatives: Let `uv run` install or update dependencies on demand; spawn a new Python process per request; implement speculative memory behavior in Phase 2.
- Why: Runtime must not mutate its control-plane environment or depend on network setup, and a long-lived bounded bridge preserves graceful vanilla operation. Reserving strict commands prevents boundary churn without misreporting an unimplemented capability.
- Affected files: `.prime/agent/extensions/synth/bridge.ts`, `synth/bridge.py`, `synth/protocol.py`, `synth/service.py`, `pyproject.toml`.

## 2026-08-26 — D-0028: Preserve reviewed prompt bytes outside line-length policy

- Decision: Ruff ignores only `E501` for the two reviewed prompt-port modules; all other rules still apply. TypeBox remains the telemetry write authority, while the Python mirror deliberately validates the same field shape and numeric constraints before CLI reads.
- Alternatives: Reflow the prompt strings and provenance lines; disable lint for the overlay; maintain unrelated JSON schemas in both languages.
- Why: Reflowing near-verbatim research contributions obscures fidelity review, whereas a narrow line-length exception preserves hashes and leaves executable code under normal lint and strict typing.
- Affected files: `pyproject.toml`, `local_memory/prompts_reasoning_bank.py`, `middleware/prompts_bats.py`, `.prime/agent/extensions/synth/telemetry.ts`, `middleware/telemetry.py`.

## 2026-08-26 — D-0029: Enforce runtime control-plane policy at the available tool boundary

- Decision: The extension blocks direct edits to Synth control-plane roots and known files, repository/dependency mutation commands, and recognizable shell/IPython writes that target those roots. This hook is a fail-closed policy layer, not an operating-system sandbox; Phase 7 adds the required independent static skill gate.
- Alternatives: Patch every core tool; disable editing and shells entirely; claim exhaustive command-string sandboxing; wait until the compiler exists.
- Why: `tool_call` is the only extension surface before execution and can preserve normal coding work while preventing ordinary self-modification paths. Symlink, indirection, and subprocess completeness require deterministic package inspection as the second enforcement layer rather than overstating regex coverage.
- Affected files: `.prime/agent/extensions/synth/guard.ts`, `.prime/agent/extensions/synth/index.ts`, `scripts/synth-phase2-demo.ts`, future Phase 7 skill gate.

## 2026-08-26 — D-0030: Merge the reviewed AutoSaddler engine below the Synth CLI head

- Decision: Merge `synth/p6-autosaddler` as the completed Phase 6 implementation, retain `synth.cli:main` as the only public Python head, and delegate `synth optimize` to `optimizer.cli`. The pinned upstream vendor tree is excluded from fork Ruff rewrites and strict mypy traversal; every non-vendored adapter remains covered, and the generic vendored CLI is statically reachable through the public construction module.
- Alternatives: Restore `optimizer.cli:main` as a third product entry point; duplicate its parser in `synth`; mechanically reformat and type-repair the reviewed vendor snapshot; rebuild Phase 6.
- Why: The operator supplied a fresh-clone-reviewed branch and an explicit CLI seam. Rewriting the vendor would invalidate that review and expand `PATCHES.md`, while static reachability plus adapter lint/type checks preserves the two-head law and a clean upstream comparison.
- Affected files: `pyproject.toml`, `synth/cli.py`, `optimizer/cli.py`, `optimizer/autosaddler.py`, `scripts/check-wiring.sh`, merged `optimizer/vendor/autosaddler_v2/**`.

## 2026-08-26 — D-0031: Classify the vendored Claude endpoint as an unreachable opt-in surface

- Decision: Allow the exact `api.anthropic.com` literal only in AutoSaddler's preserved optional Claude provider file. The shipped Synth optimizer config uses the deterministic stub, and `optimizer.runtime` accepts only `stub` or `prime_agent`, so the generic vendor registry cannot select Claude through `synth optimize`.
- Alternatives: Delete the optional upstream provider; weaken the classifier for the entire vendor directory; permit Claude in the Synth scenario dispatcher.
- Why: The operator required the reviewed complete v2 vendor snapshot, while Law 3 prohibits a vendor dependency in Synth's runtime path rather than forbidding preserved opt-in source. Exact-file classification and a negative dispatcher assertion keep that distinction executable.
- Affected files: `tests/harness_regression/test_no_vendor_hosts.py`, merged `optimizer/vendor/autosaddler_v2/providers/claude.py`, `optimizer/runtime.py`, `optimizer/configs/synth_local.yaml`.

## 2026-08-26 — D-0032: Translate BATS units while preserving its budget protocol and strategic bands

- Decision: Preserve the Budget Tracker form of the `<budget>` block, including the exact `Make the best use of the available resources.` sentence, while replacing Query/URL with Bash/Edit/IPython/Subagent/Token/Cost fields. One coding tool call or actual subagent spawn consumes one class unit. A single regime uses the minimum fraction across bounded resources with descending boundary precedence: HIGH at 70% or more, MEDIUM at 30% or more, LOW at 10% or more, and CRITICAL below 10% or when any bounded resource is zero. Tool classes remain independent; depletion denies that class, and wrap-up begins when all configured finite tool classes are depleted or a hard token/cost ceiling is reached. The regime and `<plan>` prose is translated to repository exploration, editing, and verification.
- Alternatives: Preserve web Query/URL labels and batched-string unit semantics; reproduce the reference driver's stop-when-either-budget-is-empty loop; let overlapping 70/30 thresholds depend on branch order; emit the web-domain `None`/approximate-answer instructions.
- Why: The BATS contribution is the visible budget protocol, dynamic regime playbook, plan history, and strategic enforcement rather than web-search tool names. Descending precedence resolves prompt overlap deterministically, independent classes fit coding tools, and honest wrap-up avoids claiming unverified code success. The tracker wording wins over `agent_bats.py`, which omits the required sentence.
- Affected files: `.prime/agent/extensions/synth/budget.ts`, `.prime/agent/extensions/synth/index.ts`, `middleware/prompts_bats.py`, `middleware/bats_tracker.py`, `synth.json`.

## 2026-08-26 — D-0033: Expose kernel subagent admission through the existing extension tool guard

- Decision: Before `rlm.run` creates a child, `AgentSession` emits the existing `tool_call` extension event with custom tool name `rlm.run`; a blocking result becomes the kernel request error. The Synth extension reserves one Subagent unit there. The enclosing IPython call is accounted separately, and denied subagent requests do not consume a Subagent unit.
- Alternatives: Regex-count `rlm(...)` text in IPython cells; leave subagent budgets informational; patch the Python RLM package; add a new extension event or daemon protocol message.
- Why: Actual spawns are authenticated kernel host requests hidden inside the IPython tool. The extension API otherwise cannot observe aliases, loops, or indirect calls, so source inspection is bypassable. Reusing the negotiated in-process `tool_call` policy seam is the smallest core patch and adds no daemon wire change.
- Affected files: `packages/coding-agent/src/core/agent-session.ts`, `packages/coding-agent/.changes/synth-bats-rlm-budget.md`, `.prime/agent/extensions/synth/budget.ts`, `.prime/agent/extensions/synth/index.ts`.

## 2026-08-26 — D-0034: Reserve budgets before work and enforce provider ceilings after native usage arrives

- Decision: Accepted tool and subagent calls are checkpointed to extension-owned telemetry before execution and remain consumed when execution fails. Session resume replays the latest strict budget snapshot; malformed recovery fails closed. Token and cost use comes only from finalized native `Usage` at assistant `message_end`, including cache read/write cost, and can block tools and force wrap-up immediately afterward. The provider response that crosses a token/cost ceiling cannot be pre-blocked. Historical tool responses are elided only in request context after a configurable number of assistant turns; the transcript and telemetry remain complete, and `N=1` reproduces the reference's newest-result-only policy.
- Alternatives: Count after tool completion; reset when telemetry is unavailable; estimate future output with a tokenizer; throw from `before_provider_request`; replace every previous observation immediately and destructively.
- Why: Pre-execution reservation prevents crash/restart over-admission, while native usage avoids double counting and tokenizer drift. The current provider hooks cannot cancel a request or know its eventual usage, so predictive hard stops require an unsupported core/provider contract. Configurable context-only retention is the requested observation-hygiene adaptation and coexists with native size truncation.
- Affected files: `.prime/agent/extensions/synth/budget.ts`, `.prime/agent/extensions/synth/index.ts`, `.prime/agent/extensions/synth/telemetry.ts`, `middleware/telemetry.py`, `pyproject.toml`, `uv.lock`, Phase 3 tests and demo.

## 2026-08-26 — D-0035: Apply the operator's standing ceremony and lane-ownership override

- Decision: Standing directive v5 permanently removes golden files, schema round-trip suites, mutation/property testing, fresh-context or self-adversarial gate reviews, full rereads of files authored in the same session, and test counts beyond roughly eight focused checks per feature. Gate evidence is the smallest behavior-bearing demo and bounded logic tests named by each phase. Reviewed parallel branches own the Phase 4 memory store, Phase 5 batch driver and candidate selection, and Phase 7 quarantine gate; the primary Synth lane will merge those branches at the instructed commit boundary and implement only their extension/CLI/product wiring.
- Alternatives: Retain the kickoff's broader ceremony; duplicate reviewed engines on `synth/dev`; keep Phase 3 prompt goldens and a separate schema parity suite.
- Why: The operator explicitly superseded conflicting kickoff ceremony and assigned reviewed, disjoint implementation lanes. Removing duplicate checks and engines keeps the work focused on executable product paths while preserving the phase demos, `npm run check`, existing regressions, boot proofs, host classifier, two-head wiring, and per-gate push/report cadence.
- Affected files: `DECISIONS.md`, Phase 3 tests and demo, future Phase 4/5/7 scope, and all remaining gate reports.

## 2026-08-26 — D-0036: Retrieve individual local memory items and keep them distinct from HarnessState

- Decision: Index and retrieve individual Title/Description/Content items with local BGE-small embeddings and sqlite-vec cosine similarity, using the canonical instruction-aware query `Instruct: {task}\nQuery: {query}`. The task description names prior software-engineering memories rather than prior query records. Inject only `strategy` items under `## Lessons from past sessions`, render explicit `success`/`fail` provenance, and persist the exact rendered section in a non-context `synth.memory.retrieval` entry. ReasoningBank does not write native HarnessState; Phase 7 reserves `meta_heuristic` items for a separate `## Problem framing` section.
- Alternatives: Reproduce query-record retrieval with Gemini/Qwen embeddings and 3072 dimensions; inject task/session provenance or force explicit discussion of every lesson; copy auto-distilled items into HarnessState.
- Why: Synth's required schema stores each lesson as an independently searchable row and its prepared local model is 384-dimensional BGE-small. Item-level top-k and normalized cosine scores preserve the research retrieval mechanism while the distinct heading and durable non-context receipt prevent auto-distilled memory from masquerading as curated harness state or duplicating prompt context.
- Affected files: `local_memory/retrieval.py`, `local_memory/store.py`, `synth/config.py`, `synth/service.py`, `.prime/agent/extensions/synth/memory.ts`, `.prime/agent/extensions/synth/index.ts`.

## 2026-08-26 — D-0037: Adapt the WebArena judge to repository evidence and fail closed on its response contract

- Decision: Preserve the ReasoningBank judge's intent/history/final-state/final-response structure and two-line Thoughts/Status result, but translate WebArena categories to repository inspection, diagnosis, modification, and test/build/diff evidence. Native `agent_status.taskState` and an externally supplied print exit code remain context-only priors. Parsing requires exactly two nonempty lines; extra prose is rejected rather than guessed.
- Alternatives: Run the web-navigation wording unchanged; let native task state or exit code decide correctness; scan a verbose response for the last status token.
- Why: Webpage, URL, and product criteria would misjudge coding trajectories, while the research contribution is independent LLM correctness judgment over observed evidence. Strict parsing prevents an ambiguous local-model response from being distilled into durable epistemic memory.
- Affected files: `local_memory/prompts_reasoning_bank.py`, `local_memory/judge.py`, `synth/model.py`, `synth/session.py`, `synth/reasoning_bank.py`, `synth/cli.py`.

## 2026-08-26 — D-0038: Enqueue shutdown distillation as a detached local CLI job

- Decision: On meaningful non-reload shutdowns, close the session bridge and spawn `uv run --project <cwd> --no-sync synth distill <session>` detached with ignored stdio. The hook records an extension-owned telemetry enqueue receipt and never awaits judging, model inference, embedding, or SQLite. The child waits for a stable v3 JSONL file, walks only its active parent chain, and uses semantic deduplication on retries. Interactive sessions pass no exit-code prior because none is persisted. Faux demos select deterministic judge/distill responses only through an explicit fixture path.
- Alternatives: Send a fire-and-forget request through the bridge and immediately close it; block shutdown until distillation completes; infer success from the final assistant or task state; require a new daemon queue protocol.
- Why: Closing a bridge rejects pending requests, and model/database latency cannot be placed on session teardown. The detached CLI is reachable from the sole Python head, inherits the prepared offline environment, degrades without breaking the agent, and keeps test fixtures out of production model selection.
- Affected files: `.prime/agent/extensions/synth/index.ts`, `synth/model.py`, `synth/session.py`, `synth/reasoning_bank.py`, `synth/cli.py`, Phase 4 demo and telemetry evidence.

## 2026-08-26 — D-0039: Defer MaTTS scaling until the core ReasoningBank gate is stable

- Decision: Phase 4 omits MaTTS parallel trajectories, contrast distillation, and sequential re-verification. Its reviewed prompt constants remain unreachable research references until the Phase 4 core gate and later batch infrastructure are complete.
- Alternatives: Add `synth matts -k N` before completing two-session retrieval and shutdown distillation; delete the reviewed stretch prompt constants.
- Why: The phase marks MaTTS as stretch-only, and standing directive v5 limits this lane to product wiring around the reviewed store. Phase 5 already owns fresh-trajectory batch scaling and is the coherent place to revisit parallel contrast induction.
- Affected files: `local_memory/prompts_reasoning_bank.py`, Phase 4 report, future Phase 5/ReasoningBank integration notes.

## 2026-08-26 — D-0040: Verify conclusions in the extension and persist a strict terminal verdict

- Decision: Run the coding-domain adaptation of `pr_chat_verification` from `agent_end`, after the terminal assistant message is durable. Require exactly the five BATS JSON fields and one of `SUCCESS|CONTINUE|PIVOT`; invalid or unavailable endpoint responses warn once and leave the agent conclusion unchanged. Persist valid decisions as non-context `synth.verifier.decision` entries and extension telemetry, and let CONTINUE/PIVOT enqueue exactly one budget-bearing follow-up while another configured verification cycle remains. `maxCycles` counts total verifier decisions, so its default of two supports attempt one PIVOT followed by attempt two SUCCESS without a third attempt. Interactive verification remains opt-in; `synth run --bats` explicitly forces the configured batch verifier while still honoring the global kill switch and cycle cap. The deterministic faux path is selected only by `SYNTH_VERIFIER_FIXTURE`.
- Alternatives: Verify at `turn_end`; invoke a second Python verifier after print mode exits; accept fenced JSON or substring verdicts; interpret `maxCycles=2` as two follow-ups plus an initial decision; treat every headless session as batch.
- Why: `agent_end` is the awaited conclusion seam and can queue a follow-up before print mode declares headless idle. A durable strict verdict prevents duplicate verification on resume and gives the batch driver one authoritative result without paying for or disagreeing with a second model call. Explicit batch environment state distinguishes Synth batch work from ordinary print/RPC sessions.
- Affected files: `.prime/agent/extensions/synth/verifier.ts`, `.prime/agent/extensions/synth/index.ts`, `scripts/synth-phase5-demo.ts`.

## 2026-08-26 — D-0041: Scale coding trajectories with aggregate Synth budget fractions

- Decision: Replace the reviewed driver's Query/File transcript artifact with all six live Synth counters. Unlimited counters are excluded; a fresh trajectory starts only when every bounded remaining fraction is strictly greater than `verifier.contGen.minBudgetFrac`. The driver carries aggregate used values into the next clean worktree through a batch-only environment receipt, and the extension initializes the new session tracker from that receipt before recording further native usage. This adapts the reference's strict Query `>5` and URL `>3` absolute thresholds to heterogeneous coding budget classes while preserving its strict boundary and global-consumption behavior. In-session CONTINUE/PIVOT belongs to the extension; Python consumes the final persisted decision, records verified patch artifacts, and selects candidates with the reference `pass/select/first` metrics. Mixed candidates use a strict diff-aware local judge; fixtures are explicit CLI inputs.
- Alternatives: Reset full budgets in every worktree; retain Query/File labels; require any one counter rather than all bounded counters to clear the threshold; have the Python driver restart a terminated CONTINUE/PIVOT session; reuse AutoSaddler candidate commits instead of Phase 5 patch artifacts.
- Why: Coding resources use different units, so a configurable fraction is the only coherent translation of the web-specific absolute cutoffs. Carrying used values makes the visible `<budget>` block and enforcement global across fresh sessions. Keeping repair inside the still-live extension session preserves context, while clean detached worktrees prevent candidate leakage and leave the active checkout untouched.
- Affected files: `.prime/agent/extensions/synth/budget.ts`, `middleware/batch_driver.py`, `middleware/batch_select.py`, `synth/config.py`, `synth/batch.py`, `synth/cli.py`, Phase 5 fixtures and tests.

## 2026-08-26 — D-0042: Replace compaction only for Synth-managed batch sessions

- Decision: In a Synth batch session, `session_before_compact` calls the configured small endpoint for a plan-preserving summary and returns a complete native compaction result. The last `<plan>...</plan>` is extracted and appended verbatim under `## Active Plan` if the model omitted it. The prompt includes the task, previous summary, latest verifier trajectory summary, and bounded conversation. Endpoint failure, timeout, abort, empty output, or a missing fixture compaction response falls back to native compaction unchanged.
- Alternatives: Mutate `customInstructions`; patch the core compactor; replace compaction for interactive sessions; trust the summarizer to reproduce the plan without deterministic preservation.
- Why: The hook result cannot replace compaction instructions, but it can safely supply a full compaction. Batch-only replacement ports BATS's previous-plan carry-forward without changing vendored core or risking normal interactive compaction.
- Affected files: `.prime/agent/extensions/synth/verifier.ts`, `.prime/agent/extensions/synth/index.ts`.

## 2026-08-26 — D-0043: Serialize compilation through native refinement and detach durable jobs

- Decision: The registered `compile_skill` tool, `/synth compile`, and the trusted project `synth-compile` kernel skill all schedule the same strict UUIDv4 marker through native `/refine`. `session_before_refine` recognizes only an unconsumed matching marker and returns a zero-edit proposal; `refine_complete` starts the compiler only after that refinement transaction ends. A session admits at most one nonterminal request, records session-wide `scheduled` and `started` receipts, launches a detached Python CLI child with durable stdout/stderr paths, and reconciles that job after reload or restart. Ordinary refinement is never skipped. Promotion is visible on the next session or after an explicit `/synth reload-skills`; the extension does not automatically reload itself.
- Alternatives: Run generation directly inside the tool hook; allow compiler and HarnessState writers concurrently; keep an awaited child tied to a stale extension context; automatically reload the entire extension/resource stack after promotion; allow generated skills to invoke refinement themselves.
- Why: D-0012 requires one mutation at a time, while native refinement is the only existing turn-end serialization seam exposed to both the kernel and extension. Detached receipts make terminal loss and reload reconcilable without blocking the agent. The committed `synth-compile` skill is a trusted loader input that only emits the marker; compiler-generated candidates remain forbidden from invoking refinement or compilation recursively.
- Affected files: `.prime/agent/extensions/synth/index.ts`, `.prime/agent/extensions/synth/compiler.ts`, `.prime/agent/skills/synth-compile/**`.

## 2026-08-26 — D-0044: Gate generated skills under the kernel's Python contract with an offline cache

- Decision: Generated packages target Python `>=3.11`, matching the managed Prime Agent kernel, while the Synth overlay remains pinned to Python 3.12. Quarantine evaluation runs Ruff and pytest in an isolated Python 3.11 uv environment with a setup-prewarmed repository cache and `UV_OFFLINE=1`. Package shape requires exactly the native skill metadata, typed `run()`, and at least two behavioral tests. Static safety rejects symlinks, undeclared network imports, unresolved or control-plane writes, direct writes to live skill/compiler lifecycle state, process/dynamic execution, recursive refinement/compilation, bundled-skill shadowing, and size-cap violations. The hidden fixture option exists only for deterministic faux acceptance and still traverses write, gate, audit, and promotion code.
- Alternatives: Run generated packages in the overlay Python 3.12 environment; let gate execution resolve dependencies online; trust an LLM safety review; bypass the promotion path for fixtures; permit subprocesses or recursive compiler helpers in generated skills.
- Why: Promoted skills execute in the kernel, not the overlay, so the gate must qualify the same minimum interpreter and remain usable after offline setup. Deterministic static and executable checks form the second Law 5 enforcement layer behind the extension tool guard. Fixtures must prove the real promotion boundary without a local model server.
- Affected files: `compiler/gate.py`, `compiler/service.py`, `scripts/setup-offline.sh`, `pyproject.toml`, `tests/skill_gate_fixtures/**`, `tests/skill_compiler_fixtures/line-normalizer.json`, `tests/harness_regression/test_skill_gate.py`.

## 2026-08-26 — D-0045: Count compiler admissions durably and make promotion and demotion hash-checked

- Decision: `skillCompiler.maxPerSession` counts durable compiler admissions, including rejected or failed attempts, rather than only promoted skills. The Python service serializes audit and lifecycle changes under a file lock, mines repeated successful tool n-grams of length two through five when no workflow is supplied, writes only into quarantine, atomically renames a passing package into `.prime/agent/skills/`, and records its gated content hash. Demotion requires an active promotion receipt and the same source hash, removes only derived Python runtime caches before verification, then moves the package to timestamped `.synth/demoted/` storage and records the evidence. The global kill switch records a disabled attempt without generating or quarantining a package.
- Alternatives: Count only successful promotions; keep caps in extension memory; copy files individually into the live skill directory; delete demoted packages; ignore post-gate source changes; hash runtime-generated bytecode caches as source.
- Why: Admission-count caps remain enforceable across crashes and make costly failed generations bounded. Locked append-and-fsync audit records, atomic promotion, and source hashes prevent partial or silently altered lifecycle transitions. Python produces `__pycache__` during legitimate invocation, so removing only those derived caches restores the original gated-source hash without weakening checks on source or other added files.
- Affected files: `compiler/service.py`, `synth/cli.py`, `synth/config.py`, `synth.json`, `.prime/agent/extensions/synth/index.ts`, Phase 7 demo and gate regressions.

## 2026-08-26 — D-0046: Adapt MaTTS contrast into one separately retrieved problem-framing item

- Decision: On an independently judged failure, retrieve one similar `success/strategy` item and contrast its distilled Title/Description/Content, original task query, and similarity score with the full failed trajectory. Generate at most one `status=fail`, `item_type=meta_heuristic` item, deduplicate only against that item type, and retrieve it independently under `## Problem framing` with a one-item cap. Ordinary lessons and framing degrade independently and persist exact rendered sections plus matching item IDs. Meta failure never rolls back or masks primary success/failure strategy distillation. A retry may repeat the bounded contrast inference before semantic deduplication skips storage. Parallel MaTTS trajectories and sequential re-verification remain omitted.
- Alternatives: Pretend the successful memory contains its full source trajectory; mix framing into `## Lessons from past sessions`; deduplicate against ordinary strategies; fail the whole distillation after a meta-only error; add Phase 4's deferred parallel scaling now.
- Why: The current schema retains distilled successful evidence rather than complete successful trajectories. This preserves the useful MaTTS contrast mechanism without overstating provenance, while separate retrieval and failure handling keep the already-gated ReasoningBank path available. Repeating inference on an explicit retry is retained because semantic equivalence cannot be known locally before generating the candidate.
- Affected files: `local_memory/prompts_reasoning_bank.py`, `local_memory/dedup.py`, `synth/reasoning_bank.py`, `synth/model.py`, `.prime/agent/extensions/synth/memory.ts`, `.prime/agent/extensions/synth/index.ts`, `scripts/synth-phase7-meta-demo.py`, `tests/harness_regression/test_memory_store.py`.

## 2026-08-26 — D-0047: Add the missing MCP subpath to the source extension-loader aliases

- Decision: Add exact `@earendil-works/pi-ai/mcp` and legacy-namespace MCP aliases beside the existing root and OAuth aliases in the Node/source-mode extension loader. This is an additional vendored-tree patch outside Appendix A. It is backward-compatible, changes no daemon command, event, response shape, capability, protocol version, or schema revision.
- Alternatives: Avoid the supported coding-agent extension API and duplicate its conversation/tool helpers in Synth; demonstrate Phase 7 only through the bundled CLI; leave source TUI/SDK extension loads degraded; patch Jiti globally.
- Why: Loading Synth through the default source launcher or SDK imports the coding-agent API, whose model registry now imports `@earendil-works/pi-ai/mcp`. Without the exact alias, Jiti prefix-rewrites it to the invalid `dist/index.js/mcp`. An extension cannot repair module resolution before its factory loads, so the three-line core alias addition is the smallest fix and is exercised by the Phase 7 SDK demo.
- Affected files: `packages/coding-agent/src/core/extensions/loader.ts`, `packages/coding-agent/.changes/synth-extension-mcp-alias.md`, `scripts/synth-phase7-demo.ts`.

## 2026-08-26 — D-0048: Bound Phase 8 to four runtime failures and one durable compiler receipt

- Decision: Phase 8 carries exactly four failure-injection tests: a real locked SQLite database, a bridge killed with `SIGKILL` mid-session, the local verifier endpoint disappearing during a concluding turn, and a tool class exhausting its budget during use. Detached compiler death is proved separately as a restart-recovery receipt in the full-loop driver. Compiler state entries call `SessionManager.flushNow()` after append so a recovered `failed` receipt reaches disk even when no assistant message has yet bypassed the native pre-model persistence guard.
- Alternatives: Add a broad failure matrix; mock SQLite lock errors; treat an in-memory compiler failure as durable; patch native session persistence for all custom entries.
- Why: Standing directive v5 requires one behavior-bearing test per named failure, and the operator separately required the compiler crash receipt. The first recovery proof exposed that native session persistence intentionally defers pre-assistant custom entries. A compiler-specific force flush is the narrow extension-level repair and avoids changing the vendored session format or global persistence policy.
- Affected files: `tests/harness_regression/test_phase8_failures.py`, `synth/service.py`, `scripts/synth-phase8-endpoint-down.ts`, `scripts/synth-phase8-compiler-crash.ts`, `.prime/agent/extensions/synth/index.ts`, `scripts/synth-demo.sh`.

## 2026-08-26 — D-0049: Verify optimizer worktrees with the prepared overlay runtime

- Decision: The Synth AutoSaddler verifier runs Ruff and the harness regression suite with the current uv-managed overlay interpreter, and passes the prepared source checkout's `tsx` executable to regressions through `SYNTH_TEST_TSX`. Candidate source, tests, working directory, imports, and changed-path enforcement still come from the isolated candidate worktree; only locked execution tooling comes from setup-owned control-plane environments.
- Alternatives: Create a fresh verifier environment containing only Ruff, pytest, PyYAML, and jsonschema; duplicate every overlay runtime dependency in a verifier-specific requirement list; skip bridge- and extension-bearing regressions inside candidate verification.
- Why: The first real Phase 8 optimizer run rejected a valid candidate because its ad hoc verifier environment lacked `sqlite_vec`, even though the prepared Synth environment and candidate code were correct. Candidate regressions now cover the full overlay and TypeScript drivers, so the verifier must execute them with the same locked dependencies qualified by `scripts/setup-offline.sh` rather than maintain a divergent partial environment. The clean worktree and candidate-path boundaries remain unchanged.
- Affected files: `optimizer/synth_harness.py`, `tests/harness_regression/test_phase8_failures.py`.

## 2026-08-26 — D-0050: Make the acceptance demo create a durable optimizer commit and schema-check its docs

- Decision: `scripts/synth-demo.sh` requires a clean exact source revision, runs the complete AutoSaddler scenario, requires a strict development improvement with fix rate 1 and regression rate 0, and proves that the resulting `synth/optimizer/*` commit is a direct child changing only the declared skill surface. It leaves the local optimizer branch and ignored run store as acceptance receipts without switching, merging, or pushing that branch. The configuration reference is generated from the exported TypeBox schema and runtime defaults, and the demo fails if the checked-in document is stale.
- Alternatives: Treat a rejected optimizer run as a smoke pass; validate only an in-memory candidate; delete optimizer evidence at demo exit; maintain the configuration table by hand.
- Why: The Definition of Victory requires an actual validation-gated durable harness patch and visible fix/regression rates, not merely engine startup. A clean-source precondition makes the parent and unchanged-worktree proofs unambiguous. Schema-generated documentation turns config drift into an executable acceptance failure while keeping `config.ts` authoritative.
- Affected files: `.prime/agent/extensions/synth/config.ts`, `scripts/generate-synth-config-doc.ts`, `docs/synth/config.md`, `scripts/synth-demo.sh`, `README.md`, `docs/synth/architecture.md`, `docs/synth/runbook.md`.

## 2026-08-26 — D-0051: Expose the existing session flush through extension contexts

- Decision: Add the existing public `SessionManager.flushNow()` method to the `ReadonlySessionManager` projection supplied in extension contexts. This is an additional one-line vendored-tree type patch outside Appendix A; it changes no runtime implementation, daemon command, event, response shape, capability, protocol version, or schema revision. Synth uses it only after appending a compiler lifecycle entry that must survive a second crash before any assistant message exists.
- Alternatives: Append transcript JSON directly from the extension; globally force every extension entry to materialize an otherwise empty session; retain only the Python job files; use an unchecked cast to a method intentionally omitted from the extension type.
- Why: Native session persistence deliberately defers ordinary custom entries until an assistant response, and the extension API has no durability action. Direct file appends would desynchronize the manager's tree, changing global `appendEntry` behavior would create unwanted empty sessions, and an unchecked implementation cast would hide the dependency. Exposing the already-public targeted operation is the smallest explicit API repair and is exercised by the detached compiler restart proof.
- Affected files: `packages/coding-agent/src/core/session-manager.ts`, `packages/coding-agent/.changes/synth-extension-session-flush.md`, `.prime/agent/extensions/synth/index.ts`, `scripts/synth-phase8-compiler-crash.ts`.

## 2026-08-26 — D-0052: Exclude the Synth data plane from root Biome discovery

- Decision: Add `.synth` to the root Biome file exclusions. Source checks continue to cover the configured package source and tests; runtime telemetry, optimizer worktrees, candidate fixture repositories, quarantine output, and generated evidence are never source inputs.
- Alternatives: Delete all optimizer evidence before every `npm run check`; force every fixture repository to inherit the outer Biome root; require developers to run checks only before using Synth.
- Why: A real optimizer run stores fixture workspaces containing their own `biome.json` below ignored `.synth/`. Biome discovers those nested roots before applying the source include set and rejects the command, making valid runtime receipts poison later source validation. Excluding the already-ignored data plane aligns the checker with Git and Law 5 while preserving durable optimizer evidence.
- Affected files: `biome.json`, optimizer and demo gate behavior.

## 2026-08-26 — D-0053: Carry the prepared overlay environment into candidate child processes

- Decision: Candidate verification exports the current uv-managed environment root as `UV_PROJECT_ENVIRONMENT` in addition to using its Python interpreter and source `tsx`. Extension-launched `uv run --project <candidate> --no-sync` bridge and compiler processes therefore use the setup-qualified dependencies and console entries while keeping the candidate as cwd, configuration root, TypeScript source, test root, and policy target.
- Alternatives: Synchronize a new overlay venv in every candidate worktree; copy or symlink `.venv` into candidates; disable bridge-bearing regressions during optimization; allow `uv --no-sync` to fall back to an empty or host environment.
- Why: The first exact-commit finale reached AutoSaddler but correctly rejected its candidate because the Phase 2 regression's bridge exited with code 2 before `ping`: an isolated Git worktree has no `.venv`, and runtime correctly forbids dependency synchronization. The prepared overlay is immutable verification tooling, candidate optimizer changes cannot modify overlay Python code, and an explicit environment handoff preserves no-sync behavior without weakening candidate source or path enforcement.
- Affected files: `optimizer/synth_harness.py`, candidate verification in `scripts/synth-demo.sh`.

## 2026-08-26 — D-0054: Use Responses for live reasoning and retain Chat Completions locally

- Decision: Add optional `endpoint.api` with the exact values `openai-completions` and `openai-responses`. Omission continues to select Chat Completions, so the checked-in localhost `synth.json` and local provider remain byte-for-byte unchanged. Live OpenAI evaluation uses Responses consistently in Prime Agent, the TypeScript strategic verifier, and the Python judge/distiller. The reviewer-owned `~/.prime/agent/models.json` entry for `gpt-5.4-mini` uses Responses and current native cost rates; no key value is persisted.
- Alternatives: Disable reasoning effort on Chat Completions; change the shipped local provider to Responses; make Responses mandatory; leave Python control-plane inference on a different protocol from the coding session.
- Why: The reviewed Chat Completions request rejected reasoning plus function tools, while official OpenAI documentation supports `gpt-5.4-mini` reasoning, function calling, and the Responses endpoint and recommends Responses for reasoning/tool workflows. An explicit protocol field keeps local OpenAI-compatible servers on the most broadly supported default while making live tool use and control-plane judgment coherent. On 2026-08-26 the official model page listed $0.75/M input, $0.075/M cached input, and $4.50/M output; those user-catalog rates make native cost telemetry meaningful.
- Affected files: `.prime/agent/extensions/synth/config.ts`, `.prime/agent/extensions/synth/verifier.ts`, `synth/config.py`, `synth/model.py`, `scripts/synth-phase5-demo.ts`, `tests/harness_regression/test_live_endpoint.py`, `docs/synth/config.md`, `docs/synth/runbook.md`, and the operator-owned `~/.prime/agent/models.json`.

## 2026-08-26 — D-0055: Score paired memory evaluation with immutable banks and deterministic acceptance

- Decision: `synth evaluate` materializes fresh committed fixture repositories without their solution-bearing `task.yaml`, snapshots one nonempty ReasoningBank database through SQLite backup, alternates memory-off and memory-on trials, and copies the same snapshot into every memory-on detached worktree. Focused pytest exit status is the ground-truth outcome and gates verifier/candidate admission. Evaluation sets `SYNTH_BATCH_READ_ONLY_MEMORY=1`, which leaves retrieval enabled but suppresses shutdown distillation. The strategic verifier uses a fixed SUCCESS fixture so trial success cannot be changed by an unmeasured model call. Reports sum native assistant usage and cost only, preserve sessions and telemetry, record treatment exposure, and label live bank-building judge/distill calls as separate unmeasured control-plane evidence.
- Alternatives: Trust the strategic verifier as the task score; copy a live SQLite file without its WAL; share one mutable database across trials; expose task manifests to the coding agent; include live verifier calls without tracking their usage; drop failed infrastructure or unexposed memory-on trials from the denominator.
- Why: A meaningful memory comparison requires identical executable acceptance, no solution leakage, one immutable treatment, and an intent-to-treat record. Native Prime Agent usage is the only priced usage persisted with each trajectory, so it is the honest basis for per-configuration token and cost numbers. The current fixtures are deliberately small, so the result is reported as a descriptive paired pilot with ceiling risk rather than a statistically powered causal claim.
- Affected files: `middleware/batch_driver.py`, `synth/effectiveness.py`, `synth/cli.py`, `.prime/agent/extensions/synth/index.ts`, `tests/batch_fixtures/fake_coding_agent.py`, `tests/harness_regression/test_batch_driver.py`, `tests/harness_regression/test_effectiveness.py`, and `docs/synth/runbook.md`.

## 2026-08-26 — D-0056: Match protected control-plane paths on path boundaries

- Decision: The IPython and shell guard still denies mutations that name protected control-plane roots or files, but relative protected names now require path-token boundaries while absolute protected paths remain exact. Ordinary task paths may therefore contain the product name `synth` in an ancestor directory without every mutating Python cell being misclassified. Edit-tool checks continue to resolve the target path directly, and repository/dependency mutation commands remain denied independently.
- Alternatives: Disable the guard during effectiveness runs; relocate every task outside `/Users/mac/synth`; allow every ambiguous Python mutation; keep substring matching and report that the coding harness cannot edit ordinary fixture files.
- Why: Live validation showed that an absolute task path under `/Users/mac/synth/.synth/...` contains the substring `synth`, so the old matcher treated a write to top-level `slug.py` as a write to the overlay package `synth/`. Law 5 protects specific control-plane paths, not arbitrary ancestors whose names contain the same characters. Boundary-aware matching preserves that policy while restoring the core coding-agent function.
- Affected files: `.prime/agent/extensions/synth/guard.ts`, `scripts/synth-phase7-demo.ts`.

## 2026-08-26 — D-0057: Bind effectiveness acceptance and evidence to immutable content

- Decision: `synth evaluate` copies focused tests into a controller-owned, read-only acceptance directory and runs them through an isolated Python process after inserting only the candidate worktree implementation path. The batch driver also verifies SHA-256 digests for the external runner/tests and the agent-visible worktree test copies before acceptance; any mutation skips pytest and candidate admission. A report is complete only when every agent session exists and pytest produced an ordinary pass or assertion-failure exit without timeout or integrity failure. Each report records an aggregate hash of all tracked and untracked source content, the tracked diff hash, untracked content hashes, and the executed CLI hash, and aborts if any changes during the run.
- Alternatives: Run the writable in-worktree tests directly; trust observed candidate diffs not to touch tests; treat all nonzero pytest or timeout exits as completed observations; identify dirty source only by commit and path-only status.
- Why: The original paired pilot happened not to modify tests, but its evaluator could not prove that property and its report could not identify the uncommitted implementation that produced it. External hashed acceptance removes the writable oracle, while content fingerprints make paid evidence reproducible and independently attributable even before the coherent source commit exists.
- Affected files: `middleware/batch_driver.py`, `synth/effectiveness.py`, `tests/batch_fixtures/fake_coding_agent.py`, `tests/harness_regression/test_batch_driver.py`, `tests/harness_regression/test_effectiveness.py`.

## 2026-08-26 — D-0058: Aggregate MaTTS parallel trajectories after sequential correctness re-verification

- Decision: `synth matts -k N` snapshots one input bank, launches `N` one-trajectory batch runs concurrently, suppresses their shutdown distillation, and then processes their durable sessions in sample order. Each session receives the standard ReasoningBank judge followed by one coding-domain adaptation of the paper's sequential self-refinement prompt; both reasoning records and the final corrected verdict enter one parallel self-contrast prompt using the pinned query and trajectory forms. The contrast parser admits the paper's maximum five items. Aggregated strategies use `status=success` when any reverified trajectory succeeds and `status=fail` only when all fail, with a synthetic MaTTS session ID and task-derived template key.
- Alternatives: Distill each trajectory independently; run only parallel contrast without sequential notes; resume and mutate a removed detached worktree; classify aggregated memories by majority vote; share a writable memory database across the parallel agents.
- Why: The paper defines parallel scaling as self-contrast across independent trajectories and sequential scaling as self-refinement whose intermediate corrections become memory signals. Prime Agent's completed batch worktrees are intentionally removed, so a second strict correctness judgment is the durable, domain-appropriate re-examination step. One immutable snapshot prevents cross-sample contamination, while success provenance requires at least one positively verified execution rather than a majority that could discard a valid strategy.
- Affected files: `local_memory/prompts_reasoning_bank.py`, `local_memory/distill.py`, `synth/model.py`, `synth/batch.py`, `synth/matts.py`, `synth/cli.py`, `scripts/synth-matts-demo.py`, `tests/harness_regression/test_matts.py`, `docs/synth/runbook.md`.

## 2026-08-26 — D-0059: Close the extract-zip residual at its only caller

- Decision: No patched `extract-zip` release exists for GHSA-jmr9-qjv8-65gv as of 2026-08-26, and `npm audit` still reports `fixAvailable: false`. The supported online Windows helper installer now rejects every ZIP entry whose Unix mode identifies a symbolic link, synchronously through `extract-zip`'s `onEntry` callback before the dependency reads the link target or creates the link. Ordinary files remain accepted, an unsafe archive fails closed, and the existing `finally` cleanup removes its archive and extraction directory. This caller guard closes the reachable D-0015 residual while the package-level advisory remains visible in dependency audits.
- Alternatives: Override to a nonexistent patched release; replace one narrow ZIP call with another extractor of uncertain symlink semantics; vendor a private dependency fork; classify the path as unreachable because Synth defaults offline.
- Why: `extract-zip@2.0.1` creates symlinks from unvalidated archive contents, while the official `fd` and `ripgrep` Windows assets require only ordinary files and directories. Rejecting links removes the vulnerable behavior without maintaining a fork. This vendored-tree security patch is explicitly operator-authorized; an extension cannot guard helper provisioning that occurs in the coding-agent runtime before extension control.
- Affected files: `packages/coding-agent/src/utils/tools-manager.ts`, `packages/coding-agent/test/tools-manager.test.ts`, `packages/coding-agent/.changes/synth-safe-zip-extraction.md`, this advisory journal.

## 2026-08-26 — D-0060: Make terminal outcomes acceptance-owned and daemon-delivered

- Decision: Every completed Synth session records at most one `session.outcome` event using the shared reward and termination-reason contract. Only immutable acceptance or an unambiguous timeout/process failure produces reward `0` or `1`; ordinary runtime conclusions, budget exhaustion, unresolved tool errors, and integrity failures retain a null reward. Batch acceptance writes an atomic durable receipt, then sends that exact receipt through an authenticated hidden `/synth outcome` command while resuming the client-owned daemon session. The extension captures and removes the one-run token from its process environment before tools execute, validates receipt identity, and remains the sole telemetry writer. A client-owned worker shutdown before delivery intentionally writes no provisional outcome; the authenticated recovery worker writes the one terminal event. The ReasoningBank judge receives the outcome only as a contextual prior, and batch effectiveness, MaTTS, and the AutoSaddler evaluator consume the same enum while retaining orchestration stop reasons separately.
- Alternatives: Infer success from a final answer, native task state, verifier decision, or process exit alone; let Python append telemetry after acceptance; rely only on a post-run file overwrite; write an UNKNOWN event before acceptance and later append a corrected second outcome; replace AutoSaddler's internal disposition taxonomy.
- Why: Deterministic rewards require controller-owned executable evidence, but telemetry has one extension-owned writer. The real private-daemon proof showed that print sessions use client-owned workers and that their startup filesystem view does not observe the controller's later receipt replacement. Authenticated daemon delivery preserves the writer boundary and single-event invariant without trusting agent-writable data. Native judge and optimizer taxonomies remain useful distinct layers, so the terminal enum is an input rather than a replacement.
- Affected files: `.prime/agent/extensions/synth/index.ts`, `.prime/agent/extensions/synth/telemetry.ts`, `middleware/terminal_outcome.py`, `middleware/telemetry.py`, `middleware/batch_driver.py`, `local_memory/judge.py`, `synth/reasoning_bank.py`, `synth/batch.py`, `synth/matts.py`, `synth/effectiveness.py`, `optimizer/synth_harness.py`, and focused regressions/demos.

## 2026-08-26 — D-0061: Use the prepared Prime Agent CLI in candidate verification

- Decision: AutoSaddler candidate verification exports the exact built source-checkout Prime Agent CLI through `SYNTH_TEST_CLI`, alongside the existing prepared `tsx` and uv environment. The private-daemon regression copies and loads the candidate worktree's Synth extension, configuration, fixtures, and tests, but invokes them through this immutable built CLI and its source-checkout `node_modules`.
- Alternatives: Build the entire vendored JavaScript workspace inside every candidate worktree; commit generated `dist/` output; omit the private-daemon lifecycle regression from candidate verification; accept a verifier failure caused solely by absent ignored build artifacts.
- Why: Git worktrees correctly contain only committed source, so they do not contain the ignored `packages/coding-agent/dist/cli.js` or root `node_modules`. The first Item 4a full-loop run therefore rejected a valid skill mutation before evaluation even though the candidate source was sound. Reusing the gate-qualified execution binary is the same immutable-tooling boundary already established for `tsx` and the overlay environment; candidate-controlled code and policy inputs remain isolated in the candidate worktree.
- Affected files: `optimizer/synth_harness.py`, `tests/harness_regression/test_batch_driver.py`.

## 2026-08-26 — D-0062: Preserve terminal failure evidence while bounding memory prompts

- Decision: ReasoningBank session parsing opts into a deterministic tool-observation redactor that retains the first 50 and last 150 lines, extracts a bounded ordered set of process exits, stack frames, and terminal exception lines from the omitted middle, and caps pathological individual lines. Judge, success/failure extraction, contrastive meta-heuristic, and MaTTS memory prompts apply the optional `memory.maxTrajectoryTokens` ceiling, default 64,000, to their total trajectory portions. The ceiling counts normalized UTF-8 bytes as a conservative tokenizer-free token upper bound. Session parsing remains unredacted by default so the Dynamic Skill Compiler retains its independently bounded raw workflow evidence.
- Alternatives: Port WebArena's first-40,000-character observation slice exactly; summarize logs with another model call; use the FastEmbed tokenizer for judge-model accounting; redact every v3-session consumer; rely only on Prime Agent's runtime tool truncation.
- Why: The reference head-only slice can discard the terminal exception that explains a coding failure, while a model summarizer adds cost, nondeterminism, and another failure mode. The embedding tokenizer need not match the configured judge model, and Synth intentionally has no second inference tokenizer. A UTF-8 byte upper bound is conservative for byte-tokenized models and makes the cap deterministic across configurable local endpoints. Opt-in session parsing confines the adaptation to ReasoningBank and MaTTS instead of weakening compiler inputs.
- Affected files: `local_memory/observation_redactor.py`, `synth/session.py`, `local_memory/judge.py`, `local_memory/distill.py`, `synth/reasoning_bank.py`, `synth/matts.py`, `.prime/agent/extensions/synth/config.ts`, `synth/config.py`, `local_memory/demo.py`, `scripts/synth-demo.sh`, focused regressions, generated config reference, `PORTING.md`, and Synth operator docs.
