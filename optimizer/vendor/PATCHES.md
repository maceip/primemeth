# AutoSaddler V2 vendor patches

Source: `microsoft/AutoSaddler` commit `30e20ce`, MIT licensed.

- Relocated `src/autosaddler/v2` to `optimizer/vendor/autosaddler_v2` and rewrote internal imports to the local namespace.
- Excluded `plugins/meta_are/` and removed its built-in registry import and registration.
- Bounded history bundles to 16 candidates, 12 iterations, 32 lesson records, 16 current cases, 64 KiB per diff, and 256 KiB total diff content.
- Added `.prime/agent/extensions/autosaddler` and `.prime/agent/skills/autosaddler-session` to ephemeral provider assets so they never become candidate changes.
- Protected `packages/`, `prime-agent-runtime/`, and `.prime/agent/extensions/` in every managed Git worktree, restored exact modes before teardown, and added scoped cleanup for mutation worktrees.
- Made retry cleanup tolerate inherited read-only control-plane modes in copied Prime Agent attempt workspaces.
- Added the upstream MIT `LICENSE` beside the vendored package.
