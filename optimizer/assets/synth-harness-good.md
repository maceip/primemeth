---
name: synth-harness
description: "Repair a diagnosed repository defect and verify the focused tests."
---

# Synth harness

`SYNTH_HARNESS_STRATEGY=repair_seeded_bug`

Inspect the failing test and implementation, make the smallest source repair, then rerun the
focused test. Do not change tests, evaluation data, packages outside the task, or unrelated files.
