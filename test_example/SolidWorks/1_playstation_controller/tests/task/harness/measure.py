"""Geometry capture from a live SolidWorks session.

Measurements are in metres, the SolidWorks API unit. Roles are assigned from
shape and position, never from body or feature names, so a remodelled part
scores the same as the reference.
"""

from __future__ import annotations

import json
import sys
import time
from itertools import combinations
from pathlib import Path

import pythoncom
import win32com.client

TASK_ROOT = Path(__file__).resolve().parents[3]
BASELINE_PATH = TASK_ROOT / "tests" / "task" / "prompt" / "input.json"

SW_DOC_PART = 1
SW_OPEN_SILENT = 1
SW_OPEN_READONLY = 2
SW_SOLID_BODY = 0
SW_SHOW_ERRORS_EVERY_REBUILD = 77
# ISldWorks::GetModeler. GetIDsOfNames does not resolve it on this install;
# the dispatch id from sldworks.tlb does.
SW_DISPID_GET_MODELER = 34
SW_DISPID_FIRST_FEATURE = 65801
SW_DISPID_FEATURE_COUNT = 66005
SW_DISPID_NEXT_FEATURE = 3
SW_DISPID_ERROR_CODE2 = 104

# Sub-15 mm^2 faces are the d-pad arrow engravings.
SMALL_FACE_M2 = 15e-6
# Grip pairs sit >80 mm apart; buttons inside one diamond sit <45 mm apart.
MIRROR_SEP_MIN_M = 0.05
# True mirrors match far tighter than this. The next-nearest distinct
# volumes in the seed (the two port bosses) differ by about 9%.
PAIR_TOL = 0.03
DIAMOND_XZ_M = 0.05
# Leftover deck buttons versus the lower port hardware.
HARDWARE_Y_GAP_M = 0.008


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def byref_long(value: int = 0):
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, int(value))


def byref_bool(value: bool = False):
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, bool(value))


def byref_variant(value=None):
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, value)


def rel_diff(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-15)


def api(obj, name: str, *args):
    """Call a SolidWorks member.

    pywin32's dynamic dispatch evaluates a no-argument method on attribute
    access, so ``face.GetArea`` is already a float and calling it raises.
    Methods that take arguments stay callable.
    """
    member = getattr(obj, name)
    if callable(member):
        return member(*args)
    if args:
        raise TypeError(f"{name} returned {type(member).__name__}, not a method")
    return member


def _find_visible(predicate) -> int | None:
    import win32gui

    found = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and predicate(win32gui.GetWindowText(hwnd)):
            found.append(hwnd)

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def _child_button(dialog: int, label: str) -> int | None:
    import win32gui

    found = []

    def visit(hwnd, _):
        if win32gui.GetClassName(hwnd) == "Button" and win32gui.GetWindowText(hwnd) == label:
            found.append(hwnd)

    win32gui.EnumChildWindows(dialog, visit, None)
    return found[0] if found else None


def _filename_edit(dialog: int) -> int | None:
    """The filename Edit sits under the ComboBox next to the File name label."""
    import win32gui

    edits = []

    def visit(hwnd, _):
        if win32gui.GetClassName(hwnd) == "Edit":
            edits.append(hwnd)

    win32gui.EnumChildWindows(dialog, visit, None)
    # The address bar also has an Edit. The filename one is the first ComboBoxEx
    # child, which EnumChildWindows reports before the address-bar edit.
    return edits[0] if edits else None


def connect():
    """Attach to the running SolidWorks, or start it.

    GetActiveObject does not see this server: SolidWorks never registers in
    the COM running-object table. Dispatch reconnects to the process already
    on screen instead of launching a second one.
    """
    sw = win32com.client.dynamic.Dispatch("SldWorks.Application")
    sw.Visible = True
    try:
        sw.UserControl = True
    except Exception:
        pass
    return sw


def get_modeler(sw):
    raw = sw._oleobj_.Invoke(SW_DISPID_GET_MODELER, 0, pythoncom.DISPATCH_METHOD, True)
    return win32com.client.dynamic.Dispatch(raw)


def invoke(obj, dispid: int, *args, kind: int = pythoncom.DISPATCH_METHOD):
    return obj._oleobj_.Invoke(dispid, 0, kind, True, *args)


def wrap(raw):
    if raw is None:
        return None
    return win32com.client.dynamic.Dispatch(raw)


class Session:
    def __init__(self):
        self.sw = connect()
        self.modeler = get_modeler(self.sw)
        self._show_errors = None
        self.last_open = {"errors": 0, "warnings": 0, "repair": False}

    def __enter__(self):
        try:
            self._show_errors = bool(
                self.sw.GetUserPreferenceToggle(SW_SHOW_ERRORS_EVERY_REBUILD)
            )
            self.sw.SetUserPreferenceToggle(SW_SHOW_ERRORS_EVERY_REBUILD, False)
        except Exception as exc:
            log(f"could not suppress rebuild dialogs: {exc}")
        return self

    def __exit__(self, *exc):
        if self._show_errors is not None:
            try:
                self.sw.SetUserPreferenceToggle(
                    SW_SHOW_ERRORS_EVERY_REBUILD, self._show_errors
                )
            except Exception:
                pass

    def open_part(self, path: Path):
        path = Path(path).resolve()
        errors = byref_long(0)
        warnings = byref_long(0)
        log(f"opening {path.name}")
        started = time.perf_counter()
        doc = self.sw.OpenDoc6(
            str(path),
            SW_DOC_PART,
            SW_OPEN_SILENT | SW_OPEN_READONLY,
            "",
            errors,
            warnings,
        )
        repair = doc is None and int(errors.value) & 2097152
        self.last_open = {
            "errors": int(errors.value),
            "warnings": int(warnings.value),
            "repair": bool(repair),
        }
        if repair:
            log("file needs repair; opening through the SolidWorks file dialog")
            doc = self._open_via_dialog(path)
        if doc is None:
            raise RuntimeError(
                f"OpenDoc6 returned nothing for {path} "
                f"(errors={int(errors.value)}, warnings={int(warnings.value)})"
            )
        log(
            f"opened in {time.perf_counter() - started:.1f}s "
            f"(errors={int(errors.value)}, warnings={int(warnings.value)})"
        )
        return doc

    def _open_via_dialog(self, path: Path):
        """OpenDoc6 refuses files flagged for repair. The file dialog does not.

        RunCommand blocks until the dialog closes, so the clicks are posted
        from a side thread. SendMessage would deadlock against that call.
        """
        import threading
        import win32con
        import win32gui

        target = str(path)
        posted = threading.Event()

        def drive() -> None:
            for _ in range(50):
                dialog = _find_visible(lambda title: title.startswith("Open"))
                if dialog:
                    edit = _filename_edit(dialog)
                    if edit:
                        win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, target)
                    button = _child_button(dialog, "Open")
                    if button:
                        win32gui.PostMessage(button, win32con.BM_CLICK, 0, 0)
                        posted.set()
                        break
                time.sleep(0.2)
            for _ in range(50):
                units = _find_visible(lambda title: title.startswith("Units"))
                if units:
                    ok = _child_button(units, "OK")
                    if ok:
                        win32gui.PostMessage(ok, win32con.BM_CLICK, 0, 0)
                    return
                time.sleep(0.2)

        worker = threading.Thread(target=drive, daemon=True)
        worker.start()
        self.sw.RunCommand(0, "")
        worker.join(timeout=5)
        doc = self.sw.ActiveDoc
        if doc is None:
            return None
        opened = str(api(doc, "GetPathName"))
        if Path(opened).name.lower() != path.name.lower():
            log(f"dialog opened {opened}, not {path.name}")
            return None
        return doc

    def close(self, doc) -> None:
        self.sw.CloseDoc(str(api(doc, "GetTitle")))


def _box_of(body) -> list[float]:
    raw = as_list(api(body, "GetBodyBox"))
    if len(raw) != 6:
        raise RuntimeError(f"expected 6 box values, got {len(raw)}")
    return [float(value) for value in raw]


def _face_records(body) -> list[dict]:
    records = []
    for face in as_list(api(body, "GetFaces")):
        area = float(api(face, "GetArea"))
        box = as_list(api(face, "GetBox"))
        if len(box) != 6:
            continue
        center = (
            (float(box[0]) + float(box[3])) / 2.0,
            (float(box[1]) + float(box[4])) / 2.0,
            (float(box[2]) + float(box[5])) / 2.0,
        )
        records.append({"area": area, "center": center})
    return records


def _body_record(body, index: int) -> dict:
    # Density 1 kg/m^3 makes the inertia a property of the solid alone.
    props = as_list(body.GetMassProperties(1.0))
    if len(props) < 12:
        raise RuntimeError(f"mass properties returned {len(props)} values")
    faces = _face_records(body)
    small = sum(1 for face in faces if face["area"] < SMALL_FACE_M2)
    return {
        "id": f"b{index:02d}",
        "name": str(api(body, "Name")),
        "centroid_m": [float(props[0]), float(props[1]), float(props[2])],
        "volume_m3": float(props[3]),
        "area_m2": float(props[4]),
        "inertia_com": {
            "Ixx": float(props[6]),
            "Iyy": float(props[7]),
            "Izz": float(props[8]),
            "Ixy": float(props[9]),
            "Izx": float(props[10]),
            "Iyz": float(props[11]),
        },
        "bbox_m": _box_of(body),
        "small_faces": small,
        "face_count": len(faces),
        "face_areas_m2": [face["area"] for face in faces],
        "faces": faces,
    }


def _mode_plane(pairs: list[tuple]) -> tuple[float, list]:
    """Mean midpoint of the largest cluster of midpoints within 0.5 mm.

    ``round`` is not usable here: two of the seed midpoints land on an exact
    half-millimetre and banker's rounding splits them from the third.
    """
    best: list = []
    for pair in pairs:
        group = [other for other in pairs if abs(other[2] - pair[2]) <= 0.0005]
        if len(group) > len(best):
            best = group
    plane = sum(item[2] for item in best) / len(best)
    return plane, best


def _cluster_diamonds(bodies: list[dict]) -> list[list[dict]]:
    """Groups of four equal-volume bodies that sit together in X-Z."""
    parent = list(range(len(bodies)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left, right in combinations(range(len(bodies)), 2):
        if rel_diff(bodies[left]["volume_m3"], bodies[right]["volume_m3"]) > PAIR_TOL:
            continue
        if rel_diff(bodies[left]["area_m2"], bodies[right]["area_m2"]) > PAIR_TOL:
            continue
        dx = bodies[left]["centroid_m"][0] - bodies[right]["centroid_m"][0]
        dz = bodies[left]["centroid_m"][2] - bodies[right]["centroid_m"][2]
        if (dx * dx + dz * dz) ** 0.5 <= DIAMOND_XZ_M:
            union(left, right)
    clustered: dict[int, list[dict]] = {}
    for index, body in enumerate(bodies):
        clustered.setdefault(find(index), []).append(body)
    diamonds = []
    for group in clustered.values():
        if len(group) != 4:
            continue
        xs = [body["centroid_m"][0] for body in group]
        zs = [body["centroid_m"][2] for body in group]
        # A real button diamond spreads in both X and Z. A row of port glyphs does not.
        if max(xs) - min(xs) < 0.010 or max(zs) - min(zs) < 0.010:
            continue
        diamonds.append(group)
    return diamonds


def _split_centre(leftovers: list[dict], deck_y: float) -> tuple[list[dict], list[dict]]:
    if len(leftovers) < 2:
        return leftovers, []
    ordered = sorted(leftovers, key=lambda body: body["centroid_m"][1])
    gaps = [
        (ordered[i + 1]["centroid_m"][1] - ordered[i]["centroid_m"][1], i)
        for i in range(len(ordered) - 1)
    ]
    gap, index = max(gaps)
    if gap < HARDWARE_Y_GAP_M:
        return leftovers, []
    lower, upper = ordered[: index + 1], ordered[index + 1 :]

    def mean_y(group: list[dict]) -> float:
        return sum(body["centroid_m"][1] for body in group) / len(group)

    if abs(mean_y(upper) - deck_y) <= abs(mean_y(lower) - deck_y):
        return upper, lower
    return lower, upper


def _pair_info(pair) -> dict:
    left, right = pair[0], pair[1]
    if left["centroid_m"][0] > right["centroid_m"][0]:
        left, right = right, left
    return {
        "left_id": left["id"],
        "right_id": right["id"],
        "separation_m": abs(right["centroid_m"][0] - left["centroid_m"][0]),
        "volume_m3": (left["volume_m3"] + right["volume_m3"]) / 2.0,
        "mid_x_m": (left["centroid_m"][0] + right["centroid_m"][0]) / 2.0,
    }


def _ids(group) -> list[str]:
    if isinstance(group, tuple):
        return [group[0]["id"], group[1]["id"]]
    return [body["id"] for body in group]


def assign_roles(bodies: list[dict]) -> dict:
    """Housing, two button diamonds, grip pairs, centre, and port hardware.

    Housing is the largest solid. Diamonds are equal-volume foursomes; the
    d-pad is the foursome with the arrow engravings. Sticks are the highest
    pair, triggers and bumpers the other pairs by volume. The centre is
    whatever small solid is left on the deck. The port bosses sit well below
    the deck and are hardware, not buttons.
    """
    if not bodies:
        raise RuntimeError("part has no solid bodies")
    largest = max(body["volume_m3"] for body in bodies)
    # A widened shell is often two solids. Both are far larger than any button.
    housing_bodies = [body for body in bodies if body["volume_m3"] >= 0.5 * largest]
    housing_ids = {id(body) for body in housing_bodies}
    rest = [body for body in bodies if id(body) not in housing_ids]

    mirror_candidates = []
    for left, right in combinations(rest, 2):
        if rel_diff(left["volume_m3"], right["volume_m3"]) > PAIR_TOL:
            continue
        if rel_diff(left["area_m2"], right["area_m2"]) > PAIR_TOL:
            continue
        separation = abs(left["centroid_m"][0] - right["centroid_m"][0])
        if separation < MIRROR_SEP_MIN_M:
            continue
        midpoint = (left["centroid_m"][0] + right["centroid_m"][0]) / 2.0
        mirror_candidates.append((left, right, midpoint, separation))
    if not mirror_candidates:
        raise RuntimeError("no mirror pairs found; cannot place the symmetry plane")
    plane, mirror_pairs = _mode_plane(mirror_candidates)
    paired_ids = {id(body) for pair in mirror_pairs for body in pair[:2]}

    unpaired = [body for body in rest if id(body) not in paired_ids]
    diamonds = _cluster_diamonds(unpaired)
    if len(diamonds) != 2:
        raise RuntimeError(f"expected 2 button diamonds, found {len(diamonds)}")
    diamonds.sort(
        key=lambda group: sum(body["small_faces"] for body in group), reverse=True
    )
    dpad, face = diamonds
    diamond_ids = {id(body) for group in diamonds for body in group}

    def pair_volume(pair) -> float:
        return (pair[0]["volume_m3"] + pair[1]["volume_m3"]) / 2.0

    def pair_z(pair) -> float:
        return (pair[0]["centroid_m"][2] + pair[1]["centroid_m"][2]) / 2.0

    sticks = max(mirror_pairs, key=pair_z)
    remaining = [pair for pair in mirror_pairs if pair is not sticks]
    remaining.sort(key=pair_volume, reverse=True)
    if len(remaining) < 2:
        raise RuntimeError(
            f"expected triggers and bumpers, found {len(remaining)} pairs"
        )
    triggers, bumpers = remaining[0], remaining[1]
    extra_pairs = remaining[2:]

    used = paired_ids | diamond_ids | housing_ids
    leftovers = [body for body in bodies if id(body) not in used]
    deck_ys = sorted(body["centroid_m"][1] for body in dpad + face)
    deck_y = deck_ys[len(deck_ys) // 2]
    centre, hardware = _split_centre(leftovers, deck_y)

    return {
        "plane_x_m": plane,
        "roles": {
            "housing": [body["id"] for body in housing_bodies],
            "dpad": _ids(dpad),
            "face_buttons": _ids(face),
            "centre": _ids(centre),
            "sticks": _ids(sticks),
            "triggers": _ids(triggers),
            "bumpers": _ids(bumpers),
        },
        "hardware_ids": [body["id"] for body in hardware],
        "extra_pair_ids": [_ids(pair) for pair in extra_pairs],
        "pairs": {
            "sticks": _pair_info(sticks),
            "triggers": _pair_info(triggers),
            "bumpers": _pair_info(bumpers),
        },
    }


def housing_signature(housing: dict, plane_x: float) -> dict:
    moment = 0.0
    area = 0.0
    for face in housing["faces"]:
        dx = face["center"][0] - plane_x
        moment += face["area"] * dx ** 3
        area += face["area"]
    return {
        "moment3_m5": moment,
        "face_area_m2": area,
        "faces": len(housing["faces"]),
        "sign": 1 if moment >= 0.0 else -1,
    }


def match_area_multiset(
    faces: list[dict], targets: list[float], rel_tol: float = 0.02
) -> list[dict] | None:
    """Greedy match of a mirror-invariant area list onto housing faces.

    Largest targets first: those areas are the least ambiguous. A face may
    be used once. Returns None when any target has no face within tolerance.
    """
    pool = list(range(len(faces)))
    used: set[int] = set()
    matched = []
    for target in sorted(targets, reverse=True):
        best_index = None
        best_err = None
        for index in pool:
            if index in used:
                continue
            err = abs(faces[index]["area"] - target) / max(target, 1e-15)
            if err <= rel_tol and (best_err is None or err < best_err):
                best_index = index
                best_err = err
        if best_index is None:
            return None
        used.add(best_index)
        matched.append(faces[best_index])
    return matched


def light_cluster(
    housing: dict, plane_x: float, target_areas: list[float]
) -> dict | None:
    matched = match_area_multiset(housing["faces"], target_areas)
    if matched is None:
        return None
    count = len(matched)
    center = [0.0, 0.0, 0.0]
    for face in matched:
        for axis in range(3):
            center[axis] += face["center"][axis]
    center = [value / count for value in center]
    return {
        "areas_m2": sorted(face["area"] for face in matched),
        "centroid_m": center,
        "side": 1 if center[0] >= plane_x else -1,
        "n_faces": len(matched),
    }


def _walk_features(doc) -> list:
    """Top-level features only. That is what GetFeatureCount reports (199 on the seed)."""
    found = []
    feat = wrap(invoke(doc, SW_DISPID_FIRST_FEATURE))
    guard = 0
    while feat is not None and guard < 5000:
        found.append(feat)
        feat = wrap(invoke(feat, SW_DISPID_NEXT_FEATURE))
        guard += 1
    return found


DANGLING_CODES = {43, 51, 63, 64}


def _error_class(code: int, type_name: str, is_warning: bool) -> str:
    """Split a feature fault into the three kinds an eval report needs.

    Dangling references, sketch faults, and everything else that blocked a rebuild.
    """
    if is_warning:
        return "warning"
    if code in DANGLING_CODES:
        return "dangling"
    if "Sketch" in type_name:
        return "sketch"
    return "rebuild"


def rebuild_census(doc) -> dict:
    # Do not rebuild. SolidWorks 2024 regenerates the housing fillet and
    # drops 24 tiny faces, so a forced rebuild no longer matches the seed.
    features = _walk_features(doc)
    errors = 0
    warnings = 0
    detail: dict[str, list] = {}
    for feat in features:
        flag = byref_bool(False)
        try:
            code = int(invoke(feat, SW_DISPID_ERROR_CODE2, flag))
            is_warning = bool(flag.value)
        except Exception:
            continue
        if code == 0:
            continue
        try:
            type_name = str(invoke(feat, 103))
        except Exception:
            type_name = ""
        name = str(api(feat, "Name"))
        kind = _error_class(code, type_name, is_warning)
        if is_warning:
            warnings += 1
        else:
            errors += 1
        detail.setdefault(name, []).append(
            {"code": code, "kind": kind, "type": type_name}
        )
    counted = len(features)
    try:
        counted = int(invoke(doc, SW_DISPID_FEATURE_COUNT))
    except Exception:
        pass
    return {
        "features": counted,
        "errors": errors,
        "warnings": warnings,
        "feature_errors": detail,
    }


def _interference_volume(body) -> float:
    props = as_list(body.GetMassProperties(1.0))
    if len(props) < 4:
        return 0.0
    return max(0.0, float(props[3]))


def interference(modeler, roles: dict, id_to_com: dict) -> dict:
    """Pairwise among controls, plus each control against the housing.

    Port hardware is excluded. On the seed that is 153 tests: C(17, 2) + 17.
    """
    control_ids = [
        body_id
        for role, ids in roles.items()
        if role != "housing"
        for body_id in ids
    ]
    housing_ids = list(roles["housing"])
    tests = list(combinations(control_ids, 2))
    tests += [(housing_id, body_id) for housing_id in housing_ids for body_id in control_ids]
    hits = []
    total = 0.0
    started = time.perf_counter()
    for index, (left_id, right_id) in enumerate(tests, start=1):
        faces_a = byref_variant(None)
        faces_b = byref_variant(None)
        solids = byref_variant(None)
        try:
            hit = modeler.CheckInterferenceBetweenTwoBodies(
                id_to_com[left_id],
                id_to_com[right_id],
                False,
                faces_a,
                faces_b,
                solids,
            )
        except Exception as exc:
            return {
                "tested_pairs": index - 1,
                "pairs": hits,
                "total_volume_m3": total,
                "error": str(exc),
            }
        if hit:
            volume = 0.0
            for solid in as_list(solids.value):
                try:
                    volume += _interference_volume(solid)
                except Exception:
                    continue
            if volume > 0.0:
                hits.append({"a": left_id, "b": right_id, "volume_m3": volume})
                total += volume
        if index % 40 == 0:
            log(f"interference {index}/{len(tests)}")
    log(
        f"interference {len(tests)} pairs in {time.perf_counter() - started:.1f}s, "
        f"overlap {total:.3e} m^3"
    )
    return {"tested_pairs": len(tests), "pairs": hits, "total_volume_m3": total}


def sketch_census(doc) -> dict:
    """Count sketches that are under- or over-defined even when the part still opens."""
    under = over = full = other = 0
    under_names: list[str] = []
    over_names: list[str] = []
    try:
        features = _walk_features(doc)
    except Exception as exc:
        return {"under": 0, "over": 0, "full": 0, "other": 0, "under_names": [], "over_names": [], "error": str(exc)}
    for feat in features:
        try:
            type_name = str(invoke(feat, 103))
        except Exception:
            continue
        if type_name not in ("ProfileFeature", "3DProfileFeature"):
            continue
        try:
            raw = invoke(feat, 76)
        except Exception:
            continue
        sketch = wrap(raw)
        if sketch is None:
            continue
        try:
            status = int(invoke(sketch, 48))
        except Exception:
            continue
        name = str(api(feat, "Name"))
        if status == 2:
            under += 1
            if len(under_names) < 4:
                under_names.append(name)
        elif status == 4:
            over += 1
            if len(over_names) < 4:
                over_names.append(name)
        elif status == 3:
            full += 1
        else:
            other += 1
    return {
        "under": under,
        "over": over,
        "full": full,
        "other": other,
        "under_names": under_names,
        "over_names": over_names,
    }


def capture_doc(sw, doc, modeler, target_areas: list[float] | None, do_interference: bool) -> dict:
    try:
        sw.CommandInProgress = True
    except Exception:
        pass

    raw_bodies = as_list(doc.GetBodies2(SW_SOLID_BODY, False))
    log(f"{len(raw_bodies)} solid bodies")
    id_to_com = {}
    bodies = []
    for index, body in enumerate(raw_bodies):
        record = _body_record(body, index)
        bodies.append(record)
        id_to_com[record["id"]] = body
        log(
            f"  {record['id']} {record['name']} V={record['volume_m3']:.6e} "
            f"faces={record['face_count']} small={record['small_faces']}"
        )

    try:
        layout = assign_roles(bodies)
    except RuntimeError as exc:
        log(f"roles failed: {exc}")
        layout = None
    if layout is None:
        boxes = [body["bbox_m"] for body in bodies] or [[0, 0, 0, 0, 0, 0]]
        bbox = [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            min(box[2] for box in boxes),
            max(box[3] for box in boxes),
            max(box[4] for box in boxes),
            max(box[5] for box in boxes),
        ]
        census = rebuild_census(doc)
        sketches = sketch_census(doc)
        stored = [{key: value for key, value in body.items() if key != "faces"} for body in bodies]
        return {
            "plane_x_m": None,
            "global": {"bbox_m": bbox, "width_m": bbox[3] - bbox[0]},
            "roles": {},
            "hardware_ids": [],
            "pairs": {},
            "bodies": stored,
            "small_face_counts": {body["id"]: body["small_faces"] for body in stored},
            "housing_signature": None,
            "light_cluster": None,
            "interference": {"tested_pairs": 0, "pairs": [], "total_volume_m3": 0.0, "skipped": True},
            "rebuild": census,
            "sketches": sketches,
            "role_error": True,
        }

    by_id = {body["id"]: body for body in bodies}
    housing = {
        "faces": [
            face
            for body_id in layout["roles"]["housing"]
            for face in by_id[body_id]["faces"]
        ]
    }
    signature = housing_signature(housing, layout["plane_x_m"])
    cluster = None
    if target_areas:
        cluster = light_cluster(housing, layout["plane_x_m"], target_areas)

    boxes = [body["bbox_m"] for body in bodies]
    bbox = [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        min(box[2] for box in boxes),
        max(box[3] for box in boxes),
        max(box[4] for box in boxes),
        max(box[5] for box in boxes),
    ]
    census = rebuild_census(doc)
    sketches = sketch_census(doc)
    log(
        f"features={census['features']} errors={census['errors']} "
        f"warnings={census['warnings']}"
    )

    overlaps = {"tested_pairs": 0, "pairs": [], "total_volume_m3": 0.0, "skipped": True}
    if do_interference and modeler is not None:
        overlaps = interference(modeler, layout["roles"], id_to_com)

    stored = []
    for body in bodies:
        kept = {key: value for key, value in body.items() if key != "faces"}
        stored.append(kept)
    return {
        "plane_x_m": layout["plane_x_m"],
        "global": {"bbox_m": bbox, "width_m": bbox[3] - bbox[0]},
        "roles": layout["roles"],
        "hardware_ids": layout["hardware_ids"],
        "pairs": layout["pairs"],
        "bodies": stored,
        "small_face_counts": {body["id"]: body["small_faces"] for body in stored},
        "housing_signature": signature,
        "light_cluster": cluster,
        "interference": overlaps,
        "rebuild": census,
        "sketches": sketches,
    }


def load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def capture_path(session: Session, path: Path, do_interference: bool = True) -> dict:
    baseline = load_baseline()
    doc = session.open_part(path)
    try:
        capture = capture_doc(
            session.sw,
            doc,
            session.modeler,
            baseline["light_cluster"]["areas_m2"],
            do_interference,
        )
    finally:
        session.close(doc)
    capture["trace"] = dict(session.last_open)
    return capture


def _names_for(document: dict, ids: list[str]) -> set[str]:
    by_id = {body["id"]: body["name"] for body in document["bodies"]}
    return {by_id[body_id] for body_id in ids}


def compare_to_baseline(capture: dict, baseline: dict) -> list[str]:
    """Name-based check used only to prove the measurer reproduces the seed."""
    mismatches = []
    by_name = {body["name"]: body for body in capture["bodies"]}
    if len(capture["bodies"]) != len(baseline["bodies"]):
        mismatches.append(
            f"body count {len(capture['bodies'])} != {len(baseline['bodies'])}"
        )
    for body in baseline["bodies"]:
        got = by_name.get(body["name"])
        if got is None:
            mismatches.append(f"missing body {body['name']}")
            continue
        for axis in range(3):
            if abs(got["centroid_m"][axis] - body["centroid_m"][axis]) > 1e-9:
                mismatches.append(
                    f"{body['name']} centroid[{axis}] "
                    f"{got['centroid_m'][axis]} != {body['centroid_m'][axis]}"
                )
                break
        if rel_diff(got["volume_m3"], body["volume_m3"]) > 1e-6:
            mismatches.append(
                f"{body['name']} volume {got['volume_m3']} != {body['volume_m3']}"
            )
        if rel_diff(got["area_m2"], body["area_m2"]) > 1e-6:
            mismatches.append(
                f"{body['name']} area {got['area_m2']} != {body['area_m2']}"
            )
        for key in ("Ixx", "Iyy", "Izz"):
            base = body["inertia_com"][key]
            if abs(base) < 1e-18:
                if abs(got["inertia_com"][key] - base) > 1e-16:
                    mismatches.append(
                        f"{body['name']} {key} {got['inertia_com'][key]} != {base}"
                    )
                    break
            elif rel_diff(got["inertia_com"][key], base) > 1e-4:
                mismatches.append(
                    f"{body['name']} {key} {got['inertia_com'][key]} != {base}"
                )
                break
    base_names = {
        role: _names_for(baseline, ids) for role, ids in baseline["roles"].items()
    }
    got_names = {
        role: _names_for(capture, ids) for role, ids in capture["roles"].items()
    }
    for role, names in base_names.items():
        if got_names.get(role) != names:
            mismatches.append(
                f"role {role}: {sorted(got_names.get(role, []))} != {sorted(names)}"
            )
    if abs(capture["plane_x_m"] - baseline["plane_x_m"]) > 1e-9:
        mismatches.append(f"plane {capture['plane_x_m']} != {baseline['plane_x_m']}")
    signature = capture["housing_signature"]
    base_signature = baseline["housing_signature"]
    if signature["faces"] != base_signature["faces"]:
        mismatches.append(
            f"housing faces {signature['faces']} != {base_signature['faces']}"
        )
    if rel_diff(signature["moment3_m5"], base_signature["moment3_m5"]) > 1e-4:
        mismatches.append(
            f"moment3 {signature['moment3_m5']} != {base_signature['moment3_m5']}"
        )
    if signature["sign"] != base_signature["sign"]:
        mismatches.append(
            f"signature sign {signature['sign']} != {base_signature['sign']}"
        )
    if capture["light_cluster"] is None:
        mismatches.append("light cluster not found")
    else:
        if capture["light_cluster"]["side"] != baseline["light_cluster"]["side"]:
            mismatches.append("light cluster side differs")
        for axis in range(3):
            got = capture["light_cluster"]["centroid_m"][axis]
            expected = baseline["light_cluster"]["centroid_m"][axis]
            if abs(got - expected) > 1e-6:
                mismatches.append(f"light centroid[{axis}] {got} != {expected}")
                break
    if capture["rebuild"]["features"] != baseline["rebuild"]["features"]:
        mismatches.append(
            f"features {capture['rebuild']['features']} != {baseline['rebuild']['features']}"
        )
    if capture["rebuild"]["errors"] != baseline["rebuild"]["errors"]:
        mismatches.append(
            f"errors {capture['rebuild']['errors']} != {baseline['rebuild']['errors']}"
        )
    return mismatches


def main(argv: list[str]) -> int:
    args = [arg for arg in argv[1:] if not arg.startswith("-")]
    do_interference = "--no-interference" not in argv
    if not args:
        raise SystemExit(
            "usage: measure.py PART.SLDPRT [--no-interference] [--out FILE]"
        )
    path = Path(args[0])
    out = None
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    with Session() as session:
        capture = capture_path(session, path, do_interference=do_interference)
    text = json.dumps(capture, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        log(f"wrote {out}")
    else:
        print(text)
    if path.name.lower() == "input.sldprt":
        mismatches = compare_to_baseline(capture, load_baseline())
        if mismatches:
            log(f"{len(mismatches)} baseline mismatches:")
            for item in mismatches[:40]:
                log(f"  {item}")
            return 1
        log("baseline match")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
