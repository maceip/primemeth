# Phase 1 Execution Plan

Phase 1 runs on `synth/dev` in the order below. Work inside each capability slice follows the standing build-first cadence: implement and wire the behavior, extend `scripts/synth-demo.sh`, run the live faux/local-config demonstration, then add only the smallest tests that protect the behavior. The inbox is checked before every commit; only explicit paths are staged. A focused commit is eligible after its named evidence, `npm run check`, `npm run build`, `scripts/proof-faux-print.sh`, and `scripts/proof-faux-tui.sh` are green.

AGENTS.md permits one changelog fragment per package per PR. The focused commits therefore share `packages/ai/.changes/synth-phase-1-local-first.md` for A3 and `packages/coding-agent/.changes/synth-phase-1-local-first.md` for A1, A2, A4, A5, and A6. Each commit adds only its own one-line, past-tense user-visible bullet to the applicable fragment.

## 1. A3 — Pin model-catalog builds

Focused commit: remove `generate-models` from the `packages/ai` build command while retaining it as an explicit script. Never edit `packages/ai/src/models.generated.ts`.

Build/demo evidence before tests:

- Record the catalog hash, run `PI_OFFLINE=1 npm run build`, and prove the hash and worktree copy are unchanged.
- Add the build assertion to the Phase 1 section of `scripts/synth-demo.sh`.

Minimal test evidence: the live build/hash assertion is the regression; no separate mock or package-script test is added.

Compatibility: build-pipeline-only; no runtime or daemon wire change.

## 2. A1 — Honor and install configured local model defaults

Focused commit: remove only the unconditional Prime Inference pre-pass from `model-resolver.ts`; preserve explicit and saved Prime defaults. Ship the `local` OpenAI-completions template, project default provider/model, and `scripts/setup-offline.sh` with the D-0014 additive atomic catalog merge, uv Python 3.12 setup, model prefetch, kernel bootstrap, and `fd`/`rg` checks.

Build/demo evidence before tests:

- Extend `scripts/synth-demo.sh` to install into an isolated configured agent directory, run setup twice, prove the second catalog merge is byte-stable, and boot the TUI against `local/local-main` without a login prompt.
- Run a faux print session through the same configured project path so no real endpoint or credential is required.

Minimal test evidence:

- Update `packages/coding-agent/test/model-resolver.test.ts` with one normal-provider-order case and one saved-local-default case; retain the existing explicit Prime regression coverage.
- Add a compact parameterized Python merge test covering D-0014's missing, additive, conflict-preserving, no-op, malformed, permissions, and atomic-cleanup outcomes without building a second setup harness.
- Run the exact model-resolver regressions and the uv-pinned merge test.

Compatibility: intentional startup fallback selection change; no daemon wire change.

## 3. A5 — Expose the optional summarizer model setting

Focused commit: add the minimal optional provider/model shape and accessor in `settings-manager.ts`. Missing or partial configuration resolves to disabled. This is an internal prerequisite for A2, not a separate product surface.

Build/demo evidence before tests: exercise the setting through the A2 live configured/no-config summarizer demonstration; no standalone mock surface is created.

Minimal test evidence: add absent, complete, and partial cases to the existing settings-manager test file, then run that one file.

Compatibility: backward-compatible optional worker setting; no daemon command, event, response, capability, protocol-version, or schema-revision change.

## 4. A2 — Configure or cleanly disable the daemon summarizer

Focused commit: remove the hardcoded Prime provider/model from `daemon-session-summarizer.ts`, resolve A5 from the worker-local settings manager, and add the local-small project setting. Missing configuration, model, authorization, endpoint, or valid response retains the existing `undefined` fallback.

Build/demo evidence before tests:

- Extend `scripts/synth-demo.sh` with one configured faux/local-small recap and one absent-setting run that proves clean no-op behavior.
- Keep recap behavior worker-local; do not add a daemon command or protocol field.

Minimal test evidence: extend the existing summarizer test with configured-local, absent/partial, and failed-resolution cases, then run it with the existing lifecycle test.

Compatibility: backward-compatible worker-local behavior; no daemon wire or startup requirement.

## 5. A4 — Make product analytics opt-in

Focused commit: default effective product telemetry to off while preserving `PI_OFFLINE` and `DO_NOT_TRACK` as absolute opt-outs and explicit environment/settings opt-in behavior. Ship the project telemetry-off setting.

Build/demo evidence before tests: the Phase 1 demo runs without an opt-in, asserts no product telemetry artifact is emitted, and then proves the existing explicit enable path still resolves enabled without sending a network request.

Minimal test evidence: add default-off and explicit-opt-in cases to the existing telemetry test and retain its override/malformed-setting cases.

Compatibility: intentional analytics-default change; no daemon wire change.

## 6. A6 — Disable vendor and remote-backed bundled skills by default

Focused commit: remove the bundled `prime-intellect` skill, set bundled websearch off in project settings, and document its explicit remote opt-in and local alternatives. Leave websearch, Linear, Notion, authentication, and MCP implementations intact; Linear and Notion remain auth-gated.

Build/demo evidence before tests: the Phase 1 demo inspects the real resolved resource set and proves `prime-intellect` is absent and websearch is absent by default while the faux print/TUI paths still boot.

Minimal test evidence: reverse the existing Prime skill assertion and add one default-off/explicit-on websearch case in the existing bundled-skill/resource tests. Reuse the existing MCP auth-gating coverage rather than adding duplicate tests.

Compatibility: intentional bundled-resource/default change; no daemon wire change.

## 7. Gate 1 — One policy regression and live acceptance

Add one small `tests/harness_regression/test_no_vendor_hosts.py` grep regression. Its exact allowlist covers documented opt-in provider/auth/catalog/websearch source paths; it separately asserts that the AI build script cannot invoke `models.dev`, project defaults are local/telemetry-off/websearch-off, and shipped default endpoint URLs are localhost. Do not build an HTTP interception harness in this phase; D-0017 defers it to Phase 8.

Gate evidence:

- `scripts/synth-demo.sh` passes its Phase 1 live flow: idempotent setup, local-catalog TUI boot with no login prompt, faux print success, configured summarizer success, missing-config no-op, analytics off, and remote-backed skills disabled.
- `npm run build` is green and leaves `packages/ai/src/models.generated.ts` unchanged.
- `npm run check` is green.
- `scripts/proof-faux-print.sh` and `scripts/proof-faux-tui.sh` are green.
- The focused existing tests named above and the one banned-host grep regression are green.
- A fresh-context adversarial reviewer verifies the live evidence, looks for unlisted vendored-tree changes and orphans, and finds no stubs reported as capability.

After the gate review, commit the remaining Gate 1 regression/report artifacts, write the Phase 1 report, and follow the control-channel cadence.
