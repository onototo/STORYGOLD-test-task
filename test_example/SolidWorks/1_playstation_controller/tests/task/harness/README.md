# Grading harness

`harness.py` scores a candidate `.SLDPRT` against `instruction.md`. The edit is a 15 mm widen of the hand-grip halves, with the button clusters re-spaced to that stance, and a left-handed swap of the D-pad, face buttons, and L/R shoulders. Text, logos, and purchased parts (joystick caps) must stay readable. A naive mirror that flips labels is considered wrong.

Grading is geometry only and independent of feature ids and body names, as required by `task.toml`. A remodelled part that lands on the same solids still scores full marks. `measure.py` is needeed for the COM capture.

`max_score` is 4, one point per component below. Each component is continuous on [0, 1]: a small error loses a little, an error as large as the tolerance loses all of it. `score` is the sum. `passed` is true only at full marks. `solution\solution.SLDPRT` scores 4. Every file under `examples\` scores less.

## Run

No path grades the document that is already active in SolidWorks. `tests\test.bat` calls the same script. Progress goes to stderr. The score JSON is the only thing on stdout.

```bat
python tests\task\harness\harness.py --capture-baseline environment\input.SLDPRT
```

prints a capture in the shape of `tests/task/prompt/input.json` and does not overwrite that file.

## How a part is read

The session is `Dispatch("SldWorks.Application")`. SolidWorks does not register in the COM running-object table, so `GetActiveObject` does not see it. A second `Dispatch` attaches to the process already on screen.

`OpenDoc6` opens the file silent and read-only (`swDocPART`). A file that returns `swFileRequiresRepairError` (2097152) is opened through the SolidWorks File Open dialog instead: the path is posted into the filename box and Open is clicked. The part is not force-rebuilt. SolidWorks 2024 regenerates the housing fillet and drops 24 small faces, so a rebuild no longer matches the frozen seed.

API lengths are metres. For every solid body (`GetBodies2`):

- `GetMassProperties(1)` with density 1 kg/m³. Volume, area, centroid, and the inertia about the centre of mass then depend on shape, not on the material the author assigned.
- `GetBodyBox` for the axis-aligned box. The part box is the union of those boxes. `width` is `xmax - xmin`.
- `GetFaces`, then `GetArea` and `GetBox` on each face. A face smaller than 15 mm² counts as a small face. Those are the D-pad arrow engravings on the seed. The box centre of a face is `(min+max)/2` on each axis.

No-argument COM methods come back as values under dynamic `pywin32` (`GetArea` is already a float). Methods that take arguments stay callable. `GetModeler` and `FirstFeature` are not visible by name on this install and are invoked by their dispatch ids from `sldworks.tlb`.

Top-level features are walked from `FirstFeature` / `GetNextFeature`. `GetFeatureCount` on the seed is 199. `GetErrorCode2` returns the error code and whether it is a warning. Only errors count toward the score. The seed has zero.

Interference is `CheckInterferenceBetweenTwoBodies` on each pair of control solids and on each control solid against the housing. Port bosses and other added hardware are left out. On the seed that is 153 tests and zero overlap. Overlap below 2e-5 m³ is treated as contact noise.

## How roles are found

Nothing is matched by the `c00` ids or by names such as `Fillet43`. The seed roles in `input.json` only supply the volumes and separations the grip pairs must still have.

The mirror plane is the mean of the X-midpoints of the stick, trigger, and bumper pairs. On the seed that is x = 80.5 mm, not x = 0. After the edit the same three pairs are found by volume within 2% and by an X-separation above 50 mm. The M3 screws sit higher in Z than the sticks and are a mirror pair of their own, so "highest pair" would pick them. Volume is used instead.

Button clusters are foursomes of equal volume (within 8%), between 0.5e-6 and 8e-6 m³, spread in both X and Z, and inside a 32 mm radius in the X-Z plane. A row of port glyphs fails the two-axis test. The two largest such foursomes are the clusters. On the seed the D-pad is the foursome with more sub-15 mm² faces.

Housing is every solid at least half the volume of the largest body. The reference shell is two bodies. The seed shell is one.

Port lights on a remodelled part are the solids under 5e-8 m³ (four domes). On the untouched seed they are still faces of the housing: the 26 areas stored in `input.json` are matched onto housing faces (areas are unchanged by a mirror), and the side is the sign of the unweighted mean of those face-box centres. The area-weighted third moment of every housing face-box centre about the plane is the shell signature. Its sign flips if the whole part is mirrored. The PlayStation mark is left-right symmetric, so its chirality is not graded.

## The four components

`widen`. Target separation of each grip pair is the seed separation plus 15 mm. The component is `1 - mean(|actual - target|) / 15 mm`, clamped to [0, 1]. A +30 mm widen and a part that was not widened both go to 0. If the pairs did not move but the part width grew by 15 mm, that bbox check is used instead, with a 5 mm tolerance. The shell can be remodelled without losing the pair check.

`layout`. Each cluster has to sit at least 88 mm from the plane (the seed offset of 80.5 mm plus the 7.5 mm outward shift of a 15 mm widen). Only a shortfall is penalised, over 7.5 mm, so a part that went out too far loses `widen` and not this component. The two foursomes must also differ in volume by more than 40%. One cluster copied onto both sides scores 0. Fewer than two foursomes scores 0.

`orientation`. The cluster on the low-X side of the plane must still carry about 42 small faces, the reference D-pad arrows. Four faces either way is full marks. None is 0. Port domes, or the matched light-face cluster, must stay on the original side of the plane (`side` +1 in `input.json`). If the two clusters are the same solid, this check is skipped so that mistake is not counted twice.

`fidelity`. `1 / (1 + new rebuild errors)`. Interference above 2e-5 m³ falls off over 5e-4 m³. A body within 0.2% of a reference housing volume (`1.002e-4 m³` / 553 faces, or `9.41e-5 m³` / 248 faces, measured on `solution.SLDPRT`) may differ by at most two faces. A larger face-count change at the same volume is an unrequested edit. A different widen changes the volume and is scored under `widen`, not here. The component is the minimum of the three.

A component at 0.995 or above is reported as 1.

## Result

```json
{"score": 3.0, "max_score": 4, "passed": false, "subscores": {"widen": 1.0, "layout": 1.0, "orientation": 0.0, "fidelity": 1.0}}
```

`examples\` holds the near misses named in `task.toml`: widen of 30 mm, widen of the shell with clusters left in place, only one cluster moved, missing glyphs, mirrored text, an extra change elsewhere, an unwidened shell, and a tree full of rebuild errors. Each of those drops the component that matches the defect. A file can drop a second component when the geometry really has a second defect, for example a widened part whose D-pad arrows are also gone.
