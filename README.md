<img width="512" src="https://github.com/user-attachments/assets/dbeb27d8-e24d-4a5e-a7a4-99fe2f8cc658" />

# PRIME METH

prime meth — the **m**utating **e**volutionary **t**rajectory **h**arness. a coding agent that runs qwen3.8-flash-next locally on apple silicon with the model's native multi-token prediction, wrapped in a harness that learns from its own trajectories and improves itself from execution traces.

## the model: qwen3.8-flash-next

[qwen3.8-flash-next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) is a multimodal moe and an early preview of the qwen4 architecture, now open-weight: 125b parameters + 51b n-gram embeddings, with just 6b activated per token. unmatched cost-efficiency.

what prime meth does with it:

- **native multi-token prediction (mtp)**: the model drafts ~4 tokens per step; drafted tokens are verified exactly, so output is token-for-token identical to one-at-a-time decoding — just faster. the runtime snapshots and restores the gated deltanet recurrent state when a draft is rejected, which ordinary kv-cache trimming cannot do. this is the piece other apple-silicon runtimes don't have.
- **large context, fast by design**: qwen sparse attention is evaluated to 1m tokens and beats full attention past 512k (ruler 93.0 vs 90.1, tech report p8); kernel-level speedups reach 7.6x prefill and 4.9x decode at 1m context, with gains starting near 64k (p9). native context is 262,144 tokens; a one-line config switch applies the documented rope-scaling recipe for the 1m window.
- **a budget system that prices the model's cost curve**: past 64k of context, long-context work gets cheaper on this architecture — and the harness's tool-call and token budgets schedule it that way.
- **on-device 4-bit mlx quantization that keeps the mtp tensors and vision tower** — the parts stock converters drop, and the reason native mtp is possible here at all.
- **runs on a mac**: one command pulls the weights, one command serves them behind an openai-compatible endpoint, one command benchmarks.

measured prefill and decode tokens/sec across the 4k → 1m context ladder, mtp on and off, land here as they are produced.

## the harness: evolutionary additions to prime agent

built on a vendored [prime agent](https://github.com/PrimeIntellect-ai/prime-agent) workspace — an rlm-native terminal coding and research harness — with four research systems integrated behind one typescript extension and one python cli (the cli is called `synth`):

- [reasoningbank](https://arxiv.org/abs/2509.25140) — "a memory mechanism for agents that learns from both successful and failed trajectories, with reasoning stored as memory content." lessons distilled from one session are retrieved into the next.
- [budget-aware tool-use (bats)](https://arxiv.org/abs/2511.17006) — "budget tracker augments a react agent with continuous awareness of its remaining tool-call budget; bats adds budget-aware planning, self-verification, and re-planning on verification failure." budgets are visible in the transcript and enforced.
- [autosaddler](https://arxiv.org/abs/2608.23041) — "automatically improves llm-agent harnesses by diagnosing execution traces, applying structured updates to prompts, tools, and middleware, and selecting changes that generalize." accepted changes land as durable commits behind a disjoint development gate.
- a dynamic skill compiler — recurring workflows become skill packages that must pass a deterministic gate (lint, tests in an ephemeral venv, static safety checks) before promotion, with per-session caps, a kill switch, and an audit log.

the full loop runs end to end (`scripts/synth-demo.sh`): a lesson distilled from a failure is retrieved in the next session; budgets are visible and enforced; the verifier pivots a failing attempt; the optimizer lands a gated commit; a compiled skill is promoted and invoked.

setup: `scripts/setup-offline.sh` · docs: `docs/synth/` · decisions journal: `DECISIONS.md`
