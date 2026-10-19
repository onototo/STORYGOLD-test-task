#!/usr/bin/env python3
"""Score frozen measurements without opening SolidWorks.

Run from this folder:

    python replay_scores.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import harness
import measure

BASELINE_PATH = Path(__file__).resolve().parents[1] / "prompt" / "input.json"


def _capture_from_seed() -> dict:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    counts = baseline.get("small_face_counts") or {}
    bodies = []
    for body in baseline["bodies"]:
        copied = dict(body)
        copied["small_faces"] = int(counts.get(body["id"], 0))
        bodies.append(copied)
    return {
        "bodies": bodies,
        "global": baseline["global"],
        "rebuild": {"errors": 0, "warnings": 0, "feature_errors": {}},
        "interference": {"total_volume_m3": 0.0},
        "sketches": {"under": 2, "over": 1, "under_names": ["Sketch1"], "over_names": ["Sketch2"]},
        "trace": {"repair": False, "errors": 0},
    }


def main() -> int:
    baseline = measure.load_baseline()
    first = harness.score_capture(_capture_from_seed(), baseline)
    second = harness.score_capture(_capture_from_seed(), baseline)
    if first != second:
        print("same measurement produced two different scores")
        return 1
    if first.get("schema") != "ps-controller-reward/1":
        print("missing reward schema")
        return 1
    if first["reward"] != round(first["score"] / first["max_score"], 4):
        print("reward is not score / max_score")
        return 1
    evidence = first["evidence"]
    if evidence["sketches_under"] != 2 or evidence["sketches_over"] != 1:
        print("sketch counts were not kept")
        return 1
    if first["subscores"]["fidelity"] != 1.0:
        print("seed joystick caps should still match", first["subscores"])
        return 1
    broken = _capture_from_seed()
    broken["rebuild"]["errors"] = 3
    for body in broken["bodies"]:
        if body["id"] in set(baseline["roles"]["sticks"]):
            body["inertia_com"] = {key: value * 1.2 for key, value in body["inertia_com"].items()}
    changed = harness.score_capture(broken, baseline)
    if not (changed["reward"] < first["reward"] and changed["subscores"]["fidelity"] < 1):
        print("a broken capture did not lose reward", changed)
        return 1
    print(json.dumps({
        "seed_reward": first["reward"],
        "seed_subscores": first["subscores"],
        "broken_reward": changed["reward"],
        "broken_fidelity": changed["subscores"]["fidelity"],
        "stick_score": changed["evidence"]["stick_score"],
    }, indent=2))
    print("replay ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
