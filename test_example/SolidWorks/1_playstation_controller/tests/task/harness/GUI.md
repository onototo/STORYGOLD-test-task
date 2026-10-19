# GUI launcher

`launch_gui.bat` opens the window and the console closes at once. Double-click it from `tests\task\harness`, or from `SolidWorks\1_playstation_controller`:

```bat
tests\task\harness\launch_gui.bat
```

SolidWorks must be installed. The window checks for `pywin32` only. If it is missing, the package name is listed and Install downloads it with pip. Nothing has to be copied into a terminal. Run harness stays disabled until that package imports.

## The list

Choose folder starts in this task (`environment`, `solution`, and `examples`, including subfolders). Every `.SLDPRT` under that folder is one row. The path of the folder is shown next to the button.

Each row has:

- A checkbox. Only ticked rows are graded. Leave a row clear to skip it.
- A thumbnail, the `.png` saved beside that part. If there is no picture, the space stays empty.
- The file name.
- The last score, or "No score yet".

A score line is `score / 4` plus `widen`, `layout`, `orientation`, and `fidelity`. A component below 1 is drawn in red. What those four numbers mean is in `README.md`. Double-click a thumbnail to open its `.png` at full size. SolidWorks is not started for that.

The thumbnail border follows the saved score:

- Green when the part scored 4 and passed.
- Red when a run finished below 4.
- No border when that part has never been run.

## Run harness

Press Run harness to grade the ticked parts one after another. The line beside the button shows which file is open. The window stays usable for scrolling; do not start a second run until the line says Done. Each finished part updates its score and border immediately. Scores are written to `scores.json` and are still there the next time the window opens. A part that has not been ticked keeps the score it already had.
