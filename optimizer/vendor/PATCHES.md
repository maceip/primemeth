# AutoSaddler V2 vendor patches

Source: `microsoft/AutoSaddler` commit `30e20ce`, MIT licensed.

- Relocated `src/autosaddler/v2` to `optimizer/vendor/autosaddler_v2` and rewrote internal imports to the local namespace.
- Excluded `plugins/meta_are/` and removed its built-in registry import and registration.
- Bounded history bundles to 16 candidates, 12 iterations, 32 lesson records, 16 current cases, 64 KiB per diff, and 256 KiB total diff content.
- Added `.prime/agent/extensions/autosaddler` and `.prime/agent/skills/autosaddler-session` to ephemeral provider assets so they never become candidate changes.
- Added the upstream MIT `LICENSE` beside the vendored package.
