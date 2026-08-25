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

The upstream AI build currently refreshes its model catalog from remote hosts. Phase 1 removes that build-time regeneration and establishes the offline path; the current baseline must not be described as offline-hardened.

Overlay Python must run through a uv-managed Python 3.12 environment. Do not use the host `python3` or an unpinned `pip` environment. `scripts/setup-offline.sh` is the Phase 1 bootstrap deliverable; until it exists, there is no supported overlay-runtime setup command.

Fetch the exact research snapshots recorded for this baseline with:

```bash
scripts/fetch-refs.sh
```

See `packages/coding-agent/docs/` for the vendored Prime Agent architecture and usage documentation.
