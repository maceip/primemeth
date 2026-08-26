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
    args.cwd.joinpath("solution.txt").write_text(
        f"trajectory {trajectory}: {args.prompt}\n"
    )
    args.session_dir.mkdir(parents=True, exist_ok=True)
    session_path = args.session_dir / f"session-{trajectory}.jsonl"
    budget_text = (
        "<budget>\n"
        f"Query Budget Used: {trajectory}, "
        f"Query Budget Remaining: {12 - trajectory}, "
        f"File Budget Used: {trajectory}, "
        f"File Budget Remaining: {8 - trajectory}\n"
        "</budget>"
    )
    records = [
        {
            "type": "session",
            "version": 3,
            "id": f"fake-session-{trajectory}",
            "cwd": str(args.cwd),
        },
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": budget_text}],
            },
        },
    ]
    session_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    print(f"fake trajectory {trajectory}: {args.prompt}")
    return int(args.fake_exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
