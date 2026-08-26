"""Deterministic print-mode coding-agent fixture for the BATS batch driver."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--print", dest="prompt", required=True)
    parser.add_argument("--fake-exit-code", type=int, default=0)
    args, _ = parser.parse_known_args()

    trajectory = int(os.environ["SYNTH_BATCH_TRAJECTORY_INDEX"])
    initial_raw = os.environ.get("SYNTH_BATCH_BUDGET_USED_JSON", "{}")
    initial_value: object = json.loads(initial_raw)
    initial = initial_value if isinstance(initial_value, dict) else {}

    def used(name: str, increment: float) -> float:
        value = initial.get(name, 0)
        return (
            float(value) + increment
            if isinstance(value, (int, float))
            else increment
        )

    bash_used = used("bash", 1)
    edit_used = used("edit", 1)
    ipython_used = used("ipython", 0)
    subagent_used = used("subagents", 0)
    token_used = used("tokens", 17)
    cost_used = used("costUsd", 0.01)
    args.cwd.joinpath("solution.txt").write_text(
        f"trajectory {trajectory}: corrected {args.prompt}\n"
    )
    args.session_dir.mkdir(parents=True, exist_ok=True)
    session_path = args.session_dir / f"session-{trajectory}.jsonl"
    budget_text = (
        "<budget>\n"
        f"Bash Budget Used: {bash_used:g}, "
        f"Bash Budget Remaining: {10 - bash_used:g}, "
        f"Edit Budget Used: {edit_used:g}, "
        f"Edit Budget Remaining: {10 - edit_used:g}, "
        f"IPython Budget Used: {ipython_used:g}, "
        f"IPython Budget Remaining: {10 - ipython_used:g}, "
        f"Subagent Budget Used: {subagent_used:g}, "
        f"Subagent Budget Remaining: {10 - subagent_used:g}, "
        f"Token Budget Used: {token_used:g}, "
        "Token Budget Remaining: unlimited, "
        f"Cost USD Budget Used: {cost_used:g}, "
        "Cost USD Budget Remaining: unlimited\n"
        "Make the best use of the available resources.\n"
        "</budget>"
    )
    records = [
        {
            "type": "session",
            "version": 3,
            "id": f"fake-session-{trajectory}",
            "cwd": str(args.cwd),
            "parentSession": None,
        },
        {
            "type": "message",
            "id": "task",
            "parentId": None,
            "timestamp": "2026-08-26T00:00:00.000Z",
            "message": {
                "role": "user",
                "content": args.prompt,
            },
        },
        {
            "type": "message",
            "id": "wrong-attempt",
            "parentId": "task",
            "timestamp": "2026-08-26T00:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "The first attempt is wrong."}],
            },
        },
        {
            "type": "custom",
            "id": "pivot-decision",
            "parentId": "wrong-attempt",
            "timestamp": "2026-08-26T00:00:02.000Z",
            "customType": "synth.verifier.decision",
            "data": {
                "verification": (
                    "The first attempt contradicts the acceptance criteria."
                ),
                "decision": "PIVOT",
                "justification": "The proposed implementation uses the wrong behavior.",
                "trajectorySummary": "Attempt one followed an invalid approach.",
                "details": {
                    "failure_analysis": "The initial approach was incorrect.",
                    "useful_information": "The target file was identified.",
                    "strategic_recommendations": (
                        "Replace the approach and verify the result."
                    ),
                },
                "assistantEntryId": "wrong-attempt",
                "cycle": 1,
                "mode": "batch",
                "task": args.prompt,
                "timestamp": 1,
            },
        },
        {
            "type": "custom_message",
            "id": "pivot-guidance",
            "parentId": "pivot-decision",
            "timestamp": "2026-08-26T00:00:03.000Z",
            "customType": "synth.verifier.guidance",
            "content": (
                "Strategic verifier: PIVOT. Replace the approach and verify the result."
            ),
            "display": True,
        },
        {
            "type": "message",
            "id": "corrected-attempt",
            "parentId": "pivot-guidance",
            "timestamp": "2026-08-26T00:00:04.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": f"Corrected attempt completed.\n{budget_text}",
                    }
                ],
            },
        },
        {
            "type": "custom",
            "id": "success-decision",
            "parentId": "corrected-attempt",
            "timestamp": "2026-08-26T00:00:05.000Z",
            "customType": "synth.verifier.decision",
            "data": {
                "verification": "The corrected attempt satisfies the fixture criteria.",
                "decision": "SUCCESS",
                "justification": "The corrected implementation is present.",
                "trajectorySummary": (
                    "Attempt one pivoted and attempt two corrected the result."
                ),
                "details": {},
                "assistantEntryId": "corrected-attempt",
                "cycle": 2,
                "mode": "batch",
                "task": args.prompt,
                "timestamp": 2,
            },
        },
    ]
    session_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    print(f"fake trajectory {trajectory}: {args.prompt}")
    return int(args.fake_exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
