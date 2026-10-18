#!/usr/bin/env python3
"""Grade a PlayStation controller part from its solid geometry.

The score does not look at feature names or body names. A candidate can
rebuild the model another way and still get full marks, as long as the
solids land on the widened, re-spaced stations measured from the seed.

What is measured, and why:

* Widen. The two sticks, the two triggers and the two bumpers are mirror
  pairs. Their separation has to grow by 15 mm. That is the grip widen the
  prompt asks for, and it still works after the shell is remodelled into
  two bodies. A shell whose outline grew by 15 mm while those pairs stayed
  put is also accepted here; the clusters are then graded on their own.
* Layout. The two button diamonds have to sit on opposite sides of the
  mirror plane, at least 7.5 mm further out than in the seed, and they have
  to be different solids. One diamond copied onto both sides fails this.
* Orientation. The d-pad keeps its arrow engravings (small faces), and the
  port lights stay on their original side of the plane. A whole-part mirror
  moves the lights; deleting the arrows removes the small faces. The PlayStation
  mark itself is symmetric, so its chirality is not graded.
* Fidelity. New rebuild errors, real solid-on-solid overlaps, and a housing
  that matches the reference volume but not its faces. The last one is the
  unrequested edit: same shell, different face count.

Each component is continuous on [0, 1]. ``score`` is their sum. ``max_score``
stays 4, matching task.toml. Run from this task's directory, with SolidWorks
open:

    python tests/task/harness/harness.py solution/solution.SLDPRT
    python tests/task/harness/harness.py
"""

from __future__ import annotations

import json
import sys
from itertools import combinations
from pathlib import Path

import measure

MAX_SCORE = 4
WIDEN_M = 0.015
PAIR_TOL = 0.02
# How far a separation or a station may miss before that check is zero.
WIDEN_TOL_M = 0.015
STATION_TOL_M = 0.0075
BBOX_TOL_M = 0.005
INTERFERENCE_TOL_M3 = 5e-4
# Reference shell, measured on solution.SLDPRT: (volume m^3, face count).
# A same-volume shell with a different face count is an unrequested edit.
REFERENCE_HOUSINGS = (
    (1.002090e-4, 553),
    (9.406909e-5, 248),
)
# Arrow engravings on the reference d-pad, summed across its four buttons.
REFERENCE_DPAD_SMALL_FACES = 42


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _snap(value: float) -> float:
    rounded = round(value, 4)
    return 1.0 if rounded >= 0.995 else rounded


def near(value: float, target: float, tolerance: float) -> float:
    if tolerance <= 0:
        return 1.0 if abs(value - target) <= 1e-9 else 0.0
    return clamp(1.0 - abs(value - target) / tolerance)


def _near_count(value: float, target: float, deadband: float = 4.0) -> float:
    error = abs(value - target)
    if error <= deadband:
        return 1.0
    span = max(target - deadband, 1.0)
    return clamp(1.0 - (error - deadband) / span)


def _baseline_pairs(baseline: dict) -> dict[str, dict]:
    by_id = {body["id"]: body for body in baseline["bodies"]}
    pairs = {}
    for role in ("sticks", "triggers", "bumpers"):
        members = [by_id[body_id] for body_id in baseline["roles"][role]]
        volume = sum(body["volume_m3"] for body in members) / len(members)
        xs = [body["centroid_m"][0] for body in members]
        pairs[role] = {
            "volume_m3": volume,
            "separation_m": max(xs) - min(xs),
        }
    return pairs


def _plane_and_grips(bodies: list[dict], baseline_pairs: dict) -> tuple[float | None, dict]:
    """Separation of each seed grip pair, found again by volume."""
    found = {}
    mids = []
    for role, spec in baseline_pairs.items():
        candidates = [
            body
            for body in bodies
            if measure.rel_diff(body["volume_m3"], spec["volume_m3"]) <= PAIR_TOL
        ]
        best = None
        for left, right in combinations(candidates, 2):
            separation = abs(left["centroid_m"][0] - right["centroid_m"][0])
            if separation < 0.05:
                continue
            midpoint = (left["centroid_m"][0] + right["centroid_m"][0]) / 2.0
            # Prefer the pair whose midpoint agrees with the other pairs.
            rank = (0 if best is None else 1, abs(midpoint - 0.0805), -separation)
            if best is None or rank < best[0]:
                best = (rank, separation, midpoint)
        if best is None:
            return None, {}
        found[role] = best[1]
        mids.append(best[2])
    return sum(mids) / len(mids), found


def _diamonds(bodies: list[dict], baseline_pairs: dict) -> list[dict]:
    """Tight foursomes of equal-volume buttons, largest first."""
    reserved = {spec["volume_m3"] for spec in baseline_pairs.values()}
    pool = []
    for body in bodies:
        if body["volume_m3"] >= 5e-5 or body["volume_m3"] <= 5e-8:
            continue
        if any(measure.rel_diff(body["volume_m3"], volume) <= PAIR_TOL for volume in reserved):
            continue
        if not 5e-7 < body["volume_m3"] < 8e-6:
            continue
        pool.append(body)
    unused = set(range(len(pool)))
    groups = []
    while len(unused) >= 4:
        best = None
        for combo in combinations(sorted(unused), 4):
            volumes = [pool[index]["volume_m3"] for index in combo]
            if max(volumes) / min(volumes) > 1.08:
                continue
            xs = [pool[index]["centroid_m"][0] for index in combo]
            zs = [pool[index]["centroid_m"][2] for index in combo]
            if max(xs) - min(xs) < 0.010 or max(zs) - min(zs) < 0.010:
                continue
            center_x = sum(xs) / 4.0
            center_z = sum(zs) / 4.0
            radius = max(
                ((xs[i] - center_x) ** 2 + (zs[i] - center_z) ** 2) ** 0.5
                for i in range(4)
            )
            if radius > 0.032:
                continue
            members = [pool[index] for index in combo]
            if best is None or radius < best[0]:
                best = (radius, combo, center_x, members, sum(volumes) / 4.0)
        if best is None:
            break
        groups.append(
            {
                "center_x": best[2],
                "volume_m3": best[4],
                "small_faces": sum(body["small_faces"] for body in best[3]),
                "members": best[3],
            }
        )
        unused -= set(best[1])
    groups.sort(key=lambda group: group["volume_m3"], reverse=True)
    return groups[:2]


def _domes(bodies: list[dict]) -> list[dict]:
    return [body for body in bodies if body["volume_m3"] < 5e-8]


def score_widen(bodies: list[dict], width_m: float, baseline: dict, pairs: dict) -> float:
    _plane, grips = _plane_and_grips(bodies, pairs)
    pair_score = 0.0
    if grips:
        errors = [
            abs(grips[role] - (pairs[role]["separation_m"] + WIDEN_M)) for role in grips
        ]
        pair_score = near(sum(errors) / len(errors), 0.0, WIDEN_TOL_M)
    base_width = baseline["global"]["width_m"]
    bbox_score = near(width_m - base_width, WIDEN_M, BBOX_TOL_M)
    return max(pair_score, bbox_score)


def score_layout(diamonds: list[dict], plane: float | None) -> float:
    if plane is None or len(diamonds) < 2:
        return 0.0
    offsets = sorted(abs(group["center_x"] - plane) for group in diamonds)
    # The seed diamonds sit 80.5 mm off the plane. The widen pushes each
    # another 7.5 mm out. Further out than that is a widen error, not a
    # missing move, so only a shortfall is penalised here.
    target = 0.0805 + WIDEN_M / 2.0
    shortfall = max(0.0, target - min(offsets))
    station = near(shortfall, 0.0, STATION_TOL_M)
    volumes = [group["volume_m3"] for group in diamonds]
    distinct = 1.0 if max(volumes) / min(volumes) > 1.4 else 0.0
    return min(station, distinct)


def score_orientation(bodies: list[dict], diamonds: list[dict], plane: float | None, capture: dict) -> float:
    port = _port_side(bodies, plane, capture)
    if len(diamonds) < 2:
        return port
    volumes = [group["volume_m3"] for group in diamonds]
    if max(volumes) / min(volumes) <= 1.4:
        # Same solid on both sides is a layout failure. Don't count it again.
        return port
    left = min(diamonds, key=lambda group: group["center_x"])
    glyphs = _near_count(left["small_faces"], REFERENCE_DPAD_SMALL_FACES)
    return min(port, glyphs)


def _port_side(bodies: list[dict], plane: float | None, capture: dict) -> float:
    domes = _domes(bodies)
    if len(domes) >= 3 and plane is not None:
        mean_x = sum(body["centroid_m"][0] for body in domes) / len(domes)
        return 1.0 if mean_x >= plane else 0.0
    cluster = capture.get("light_cluster")
    if cluster and cluster.get("side") == 1:
        return 1.0
    signature = capture.get("housing_signature") or {}
    # The untouched shell keeps a positive third moment. A whole-part mirror flips it.
    if signature.get("sign") == 1:
        return 1.0
    if signature.get("sign") == -1 and not domes:
        return 0.0
    return 1.0 if plane is not None else 0.0


def score_fidelity(bodies: list[dict], capture: dict) -> float:
    errors = int(capture.get("rebuild", {}).get("errors") or 0)
    health = 1.0 / (1.0 + errors)
    overlap = float(capture.get("interference", {}).get("total_volume_m3") or 0.0)
    if capture.get("interference", {}).get("skipped") or overlap <= 2e-5:
        overlap_score = 1.0
    else:
        overlap_score = near(overlap, 0.0, INTERFERENCE_TOL_M3)
    housing_score = _housing_faces(bodies)
    return min(health, overlap_score, housing_score)


def _housing_faces(bodies: list[dict]) -> float:
    """Penalise a shell that has the reference volume but a new face count.

    A different widen changes the volume, and that is scored as widen.
    One or two faces of noise are ignored. A real extra cut is not.
    """
    deltas = []
    for volume, faces in REFERENCE_HOUSINGS:
        matches = [
            body
            for body in bodies
            if measure.rel_diff(body["volume_m3"], volume) <= 0.002
        ]
        if not matches:
            continue
        closest = min(matches, key=lambda body: abs(body["face_count"] - faces))
        deltas.append(abs(closest["face_count"] - faces))
    if not deltas:
        return 1.0
    delta = max(deltas)
    if delta <= 2:
        return 1.0
    return clamp(1.0 - (delta - 2) / 6.0)


def score_capture(capture: dict, baseline: dict | None = None) -> dict:
    baseline = baseline or measure.load_baseline()
    pairs = _baseline_pairs(baseline)
    bodies = capture.get("bodies") or []
    plane, _grips = _plane_and_grips(bodies, pairs)
    diamonds = _diamonds(bodies, pairs)
    subscores = {
        "widen": score_widen(bodies, capture.get("global", {}).get("width_m", 0.0), baseline, pairs),
        "layout": score_layout(diamonds, plane),
        "orientation": score_orientation(bodies, diamonds, plane, capture),
        "fidelity": score_fidelity(bodies, capture),
    }
    subscores = {name: _snap(value) for name, value in subscores.items()}
    score = round(sum(subscores.values()), 4)
    return {
        "score": score,
        "max_score": MAX_SCORE,
        "passed": score >= MAX_SCORE - 1e-3,
        "subscores": subscores,
    }


def grade_path(path: Path | None) -> dict:
    with measure.Session() as session:
        if path is None:
            doc = session.sw.ActiveDoc
            if doc is None:
                raise SystemExit("no path given and SolidWorks has no active document")
            capture = measure.capture_doc(
                session.sw,
                doc,
                session.modeler,
                measure.load_baseline()["light_cluster"]["areas_m2"],
                True,
            )
            label = str(measure.api(doc, "GetPathName") or measure.api(doc, "GetTitle"))
        else:
            capture = measure.capture_path(session, path, do_interference=True)
            label = str(path)
    envelope = score_capture(capture)
    envelope["candidate"] = label
    return envelope


def main(argv: list[str]) -> int:
    args = [arg for arg in argv[1:] if not arg.startswith("-")]
    if "--capture-baseline" in argv:
        if not args:
            raise SystemExit("usage: harness.py --capture-baseline PART.SLDPRT")
        with measure.Session() as session:
            capture = measure.capture_path(session, Path(args[0]), do_interference=True)
        print(json.dumps(capture, indent=2))
        return 0
    path = Path(args[0]) if args else None
    envelope = grade_path(path)
    print(json.dumps(envelope, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
