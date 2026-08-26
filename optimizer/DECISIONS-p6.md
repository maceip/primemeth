# Phase 6 decisions

## 2026-08-25 — Preserve the V2 engine boundary

The pinned AutoSaddler V2 engine remains the durable outer loop. Synth-specific scenario, transport, evaluator, reporting, and CLI code live outside the vendor under `optimizer/`.

## 2026-08-25 — Relocate instead of aliasing the upstream namespace

Vendored imports use `optimizer.vendor.autosaddler_v2` so the fork cannot accidentally resolve an installed `autosaddler` distribution. The mechanical import rewrite is recorded in `optimizer/vendor/PATCHES.md`.

## 2026-08-25 — Keep provider session assets outside the candidate surface

Prime Agent optimizer sessions use `.autosaddler/` assets and reserved AutoSaddler paths under `.prime/agent`. Candidate steering remains limited to `.prime/agent/skills/**`, `.prime/agent/extensions/synth/**`, and explicit overlay prompt/config roots.

## 2026-08-25 — Use one transport boundary for real and smoke execution

`PrimeAgentTransport` owns real print-mode execution. The deterministic stub implements the same transport contract and handles both optimizer sessions and tiny fixture tasks during this worktree smoke, so no built Prime Agent or model endpoint is required here.

## 2026-08-25 — Update optimizer branches without switching this worktree

The CLI creates a local `synth/optimizer/<timestamp>` ref at the clean source commit, runs candidates in detached Git worktrees, and advances that ref only after matched-train acceptance and the development gate. The Phase 6 worktree stays on `synth/p6-autosaddler`.

## 2026-08-25 — Use an isolated uv cache in the managed workspace

The sandbox cannot write the default user uv cache. Phase 6 validation sets `UV_CACHE_DIR=/tmp/synth-p6-uv-cache` while retaining uv-managed Python 3.12 and leaving `uv.lock` unchanged.

## 2026-08-25 — Do not provision Node dependencies for the isolated smoke

`npm run check` cannot start because `node_modules` is absent (`biome: command not found`). The Phase 6 brief explicitly says not to build Node dependencies in this worktree, so validation uses the required Python Ruff and harness-regression gates and records the Node check as a known gap.

## 2026-08-25 — Preserve optional-provider lazy imports in vendored code

The pinned V2 Claude and Copilot providers retain their upstream function-local SDK imports so importing the vendored core does not require those optional SDKs. Synth does not use either provider; its production path is `PrimeAgentTransport`.

## 2026-08-25 — Keep the seeded bad skill in test fixtures

The deliberately bad baseline skill lives under `tests/harness_seed/`, not the repository's live `.prime/` tree. Evaluation overlays that seed only when a candidate has no target skill; a gated optimizer candidate may then add the real allowlisted skill path on its own optimizer branch.

## 2026-08-25 — Provision harness tools through isolated uv runs

Candidate verification and fixture scoring provision Ruff and pytest with isolated `uv run` environments on uv-managed Python 3.12. The regression gate also provisions the overlay's declared PyYAML and jsonschema runtime requirements. This keeps both mandatory gates and task scoring functional when the `synth` console script was installed without development extras, without updating the intentionally merge-owned `uv.lock`.
