<img width="514" src="https://github.com/user-attachments/assets/b471b3d3-bc05-4872-8f34-3866c034c3cb" />

# Synth

Synth is a local, self-hostable coding-agent harness being built on a vendored [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) workspace. The vendored baseline is upstream commit `9bc005574`; Synth development is on `synth/dev`.

## Phase 0 status

Only the reconnaissance and baseline scaffold exist. The Python files under `local_memory/`, `middleware/`, and `optimizer/` are docstring-only placeholders; the SQL file is noncanonical; and `tests/harness_regression/` contains no behavioral tests yet. ReasoningBank, BATS, AutoSaddler, the Dynamic Skill Compiler, the `synth` extension, and the `synth` CLI are not implemented in Phase 0.

The reviewed implementation plan is [INTEGRATION_MAP.md](INTEGRATION_MAP.md). Verified upstream anchors and drift are in [ARCHITECTURE_NOTES.md](ARCHITECTURE_NOTES.md), and implementation-boundary decisions are append-only in [DECISIONS.md](DECISIONS.md).

## Layout

```text
synth/
├── packages/                    # Vendored Prime Agent Node workspace
├── prime-agent-runtime/         # Vendored Python RLM runtime
├── local_memory/                # Phase 4 ReasoningBank scaffold
├── middleware/                  # Phase 2/3 telemetry and BATS scaffold
├── optimizer/                   # Phase 6 AutoSaddler scaffold
├── tests/harness_regression/    # Future D_dev regression suite
├── scripts/fetch-refs.sh        # Fetches pinned, ignored research snapshots
├── ARCHITECTURE_NOTES.md
├── INTEGRATION_MAP.md
├── DECISIONS.md
└── pyproject.toml               # Overlay package metadata; no CLI entry yet
```

Runtime databases and generated state belong under ignored `.synth/`. Reference repositories belong under ignored `refs/`. Model weights must remain outside the repository or below `.synth/`.

## Environment boundary

The Node baseline uses Node 24 and npm 11:

```bash
npm install
npm run build
```

The AI build uses its pinned model catalog and does not regenerate it from remote hosts. Phase 1 establishes the remaining local-first defaults; the branch is not fully offline-hardened until that gate is complete.

Overlay Python must run through a uv-managed Python 3.12 environment. Do not use the host `python3` or an unpinned `pip` environment.

Run the one-time local setup from the repository root:

```bash
scripts/setup-offline.sh
```

The script installs the locked Python 3.12 overlay, provisions Prime Agent's separate Python 3.11 kernel, prefetches the local embedding model, and additively merges the tracked `local` provider into the active agent-directory `models.json`. It never replaces existing provider values or model definitions. Changed JSONC catalogs are normalized to JSON; a semantic no-op preserves the original file bytes and timestamp.

The shipped project settings select `local/local-main`. The tracked template points at `http://localhost:8000/v1`; edit the installed agent-directory catalog if the local OpenAI-compatible endpoint uses a different port or model IDs. `SYNTH_LOCAL_API_KEY` is a configuration reference, not a vendor credential; local servers that ignore authentication may use any nonempty value.

Fetch the exact research snapshots recorded for this baseline with:

```bash
scripts/fetch-refs.sh
```

See `packages/coding-agent/docs/` for the vendored Prime Agent architecture and usage documentation.
