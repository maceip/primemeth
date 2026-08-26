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
