<img width="514" src="https://github.com/user-attachments/assets/b471b3d3-bc05-4872-8f34-3866c034c3cb" />

synth is a vendored [prime agent](https://github.com/PrimeIntellect-ai/prime-agent) workspace — an rlm-native terminal coding and research harness — modified to integrate three research systems and a quarantine-gated skill compiler behind one typescript extension and one python cli. it learns from both successful and failed trajectories, tracks tool-call budgets in the transcript, and improves its own harness from execution traces, with runtime and optimizer state kept local.

## upstream

[prime agent](https://github.com/PrimeIntellect-ai/prime-agent), vendored at upstream commit `9bc005574`. rlm-native terminal coding and research harness.

## papers

- [reasoningbank: scaling agent self-evolving with reasoning memory](https://arxiv.org/abs/2509.25140) — "a memory mechanism for agents that learns from both successful and failed trajectories, with reasoning stored as memory content."
- [budget-aware tool-use enables effective agent scaling](https://arxiv.org/abs/2511.17006) — "budget tracker augments a react agent with continuous awareness of its remaining tool-call budget; bats adds budget-aware planning, self-verification, and re-planning on verification failure."
- [autosaddler: automatic harness optimization with durable updates from agent execution traces](https://arxiv.org/abs/2608.23041) — "automatically improves llm-agent harnesses by diagnosing execution traces, applying structured updates to prompts, tools, and middleware, and selecting changes that generalize."

## what it can do so far

- distills lessons from successful and failed v3 session trajectories, stores them in local sqlite plus sqlite-vec, and retrieves them into the next related session
- shows per-tool, token, and cost budgets in the transcript, changes execution regimes as resources fall, denies exhausted calls, and elides stale observations
- verifies conclusions as `success`, `continue`, or `pivot`; in batch mode runs fresh worktree trajectories, records verified candidates, and selects the best result with pass, select, and first-attempt metrics
- optimizes the harness through matched training mini-batches and a disjoint development gate, creating durable accepted commits only on `synth/optimizer/*` branches
- compiles recurring workflows into skill packages that must pass a deterministic quarantine gate (lint, tests in an ephemeral venv, static safety checks) before promotion, with per-session caps, a kill switch, and an audit log
- runs the full loop end to end on a faux or configured openai-compatible endpoint: a lesson distilled from a failure is retrieved in the next session, budgets are visible and enforced, the verifier pivots a failing attempt, the optimizer lands a gated commit, and a compiled skill is promoted and invoked (`scripts/synth-demo.sh`)

setup: `scripts/setup-offline.sh` · docs: `docs/synth/` · decisions journal: `DECISIONS.md`
