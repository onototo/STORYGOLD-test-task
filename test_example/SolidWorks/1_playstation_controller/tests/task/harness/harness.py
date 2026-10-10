#!/usr/bin/env python3
"""Grading harness for 1_playstation_controller -- STUB. Write the real checks.

Usage (Windows, with SolidWorks running):
    python tests/task/harness/harness.py [candidate path]

Prints a JSON score envelope to stdout:
    {"score": ..., "max_score": 4, "passed": ..., "subscores": {...}}
"""
import json
import sys

MAX_SCORE = 4  # keep equal to max_score in task.toml


def main(argv):
    candidate = argv[1] if len(argv) > 1 else None
    envelope = {
        "score": 0,
        "max_score": MAX_SCORE,
        "passed": False,
        "subscores": {},
        "candidate": candidate,
        "note": "stub harness: no checks implemented yet",
    }
    print(json.dumps(envelope, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
