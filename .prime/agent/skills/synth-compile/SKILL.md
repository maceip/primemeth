---
name: synth-compile
description: Schedule a reusable workflow for Synth's quarantine-gated compiler. Use when a session reveals a recurring multi-step coding workflow. Do not use when the work is one-off, trivial, unsafe, or still changing.
---

# Synth Compile

Use this trusted affordance only to schedule compilation after the current IPython cell. It does not generate or promote code itself; the Synth extension serializes the request through the native refinement lifecycle and the `synth` CLI owns generation, gating, audit, and promotion.

```python
from synth_compile import run

await run(
    "Read the failing test, trace the implementation, apply the smallest fix, and rerun the focused test.",
    name_hint="focused-regression-fix",
    reason="The same verified sequence occurred repeatedly in this session.",
)
```

The workflow must describe at least two reusable steps. Do not include secrets, task-specific literal data, or instructions to modify Synth's control plane.
