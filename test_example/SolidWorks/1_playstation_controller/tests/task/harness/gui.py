#!/usr/bin/env python3
"""Small window that runs harness.py on chosen .SLDPRT files.

Scores are kept in scores.json next to this file and survive a restart.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
TASK_ROOT = HERE.parents[2]
SCORES_PATH = HERE / "scores.json"
PIP_PACKAGE = "pywin32==306"
THUMB = 256


def pywin32_status() -> str | None:
    """None when pywin32 imports. Otherwise a short reason."""
    try:
        import win32com.client  # noqa: F401
    except ImportError as exc:
        return str(exc)
    return None


def load_scores() -> dict:
    if not SCORES_PATH.is_file():
        return {}
    try:
        data = json.loads(SCORES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_scores(scores: dict) -> None:
    SCORES_PATH.write_text(json.dumps(scores, indent=2), encoding="utf-8")


def find_parts(folder: Path) -> list[Path]:
    found = [path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() == ".sldprt"]
    return sorted(found, key=lambda path: str(path).lower())


def sibling_png(part: Path) -> Path | None:
    for candidate in part.parent.iterdir():
        if candidate.is_file() and candidate.stem.lower() == part.stem.lower() and candidate.suffix.lower() == ".png":
            return candidate
    return None


def border_color(envelope: dict | None) -> str | None:
    """Green at full marks, red after a failed run, none when never scored."""
    if not isinstance(envelope, dict) or "score" not in envelope:
        return None
    full = float(envelope.get("max_score") or 4)
    if envelope.get("passed") or float(envelope.get("score") or 0) >= full - 1e-3:
        return "#2e7d32"
    return "#c62828"


def format_score(envelope: dict) -> str:
    subs = envelope.get("subscores") or {}
    bits = "  ".join(f"{name} {subs.get(name, 0):.2f}" for name in ("widen", "layout", "orientation", "fidelity"))
    return f"{envelope.get('score', 0):.2f} / {envelope.get('max_score', 4)}    {bits}"


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PlayStation controller harness")
        self.geometry("980x820")
        self.scores = load_scores()
        self.folder = TASK_ROOT if TASK_ROOT.is_dir() else HERE
        self._photos: list[tk.PhotoImage] = []
        self._rows: list[dict] = []
        self._busy = False

        self._build()
        self.refresh_dependencies()
        self.reload_parts()

    def _build(self) -> None:
        deps = ttk.LabelFrame(self, text="pywin32")
        deps.pack(fill="x", padx=8, pady=8)
        self.dep_status = ttk.Label(deps, text="")
        self.dep_status.pack(anchor="w", padx=8, pady=(6, 2))
        self.missing_box = ttk.Frame(deps)
        ttk.Label(self.missing_box, text=f"Missing: {PIP_PACKAGE}").pack(anchor="w")
        self.install_button = ttk.Button(
            self.missing_box, text="Install", command=self.install_pywin32
        )
        self.install_button.pack(anchor="w", pady=(4, 0))

        folder_row = ttk.Frame(self)
        folder_row.pack(fill="x", padx=8)
        ttk.Button(folder_row, text="Choose folder", command=self.choose_folder).pack(side="left")
        self.folder_label = ttk.Label(folder_row, text="")
        self.folder_label.pack(side="left", padx=8)

        list_frame = ttk.Frame(self)
        list_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas = tk.Canvas(list_frame, highlightthickness=0)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.parts = ttk.Frame(self.canvas)
        self.parts.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.parts, anchor="nw")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=8, pady=(0, 8))
        self.run_button = ttk.Button(actions, text="Run harness", command=self.run_selected)
        self.run_button.pack(side="left")
        self.progress = ttk.Label(actions, text="")
        self.progress.pack(side="left", padx=8)

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def refresh_dependencies(self) -> None:
        missing = pywin32_status()
        if missing is None:
            self.dep_status.configure(text="pywin32 is installed.")
            self.missing_box.pack_forget()
        else:
            self.dep_status.configure(text="pywin32 is not installed. The harness cannot start without it.")
            self.missing_box.pack(fill="x", padx=8, pady=(0, 6))
        self._sync_run_button()

    def install_pywin32(self) -> None:
        if self._busy:
            return
        self._set_busy(True, "Installing pywin32...")
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", PIP_PACKAGE],
            capture_output=True,
            text=True,
        )
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        detail = tail[-1] if tail else f"exit {completed.returncode}"
        self.after(0, lambda: self._install_done(completed.returncode == 0, detail))

    def _install_done(self, ok: bool, detail: str) -> None:
        self._set_busy(False, "")
        self.refresh_dependencies()
        if ok and pywin32_status() is None:
            messagebox.showinfo("pywin32", "pywin32 is installed.")
        else:
            messagebox.showerror("pywin32", detail)

    def choose_folder(self) -> None:
        if self._busy:
            return
        picked = filedialog.askdirectory(initialdir=str(self.folder))
        if not picked:
            return
        self.folder = Path(picked)
        self.reload_parts()

    def reload_parts(self) -> None:
        self.folder_label.configure(text=str(self.folder))
        for child in self.parts.winfo_children():
            child.destroy()
        self._photos.clear()
        self._rows.clear()
        if not self.folder.is_dir():
            ttk.Label(self.parts, text="Folder not found.").pack(anchor="w")
            return
        parts = find_parts(self.folder)
        if not parts:
            ttk.Label(self.parts, text="No .SLDPRT files in this folder.").pack(anchor="w")
            return
        for part in parts:
            self._add_row(part)

    def _add_row(self, part: Path) -> None:
        frame = ttk.Frame(self.parts)
        frame.pack(fill="x", pady=10)
        checked = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, variable=checked).pack(side="left", padx=4)
        photo = self._thumbnail(sibling_png(part))
        icon = tk.Frame(frame, bd=0, highlightthickness=0)
        icon.pack(side="left")
        tk.Label(icon, image=photo, bd=0).pack()
        self._paint_border(icon, self.scores.get(str(part.resolve())))
        text = ttk.Frame(frame)
        text.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(text, text=part.name).pack(anchor="w")
        score = ttk.Label(text, text=self._score_text(part))
        score.pack(anchor="w")
        self._rows.append({"path": part, "checked": checked, "score": score, "icon": icon})

    def _thumbnail(self, png: Path | None) -> tk.PhotoImage:
        blank = tk.PhotoImage(width=THUMB, height=THUMB)
        self._photos.append(blank)
        if png is None:
            return blank
        try:
            image = tk.PhotoImage(file=str(png))
        except tk.TclError:
            return blank
        factor = max(1, max(image.width(), image.height()) // THUMB)
        if factor > 1:
            image = image.subsample(factor, factor)
        self._photos.append(image)
        return image

    def _paint_border(self, icon: tk.Frame, envelope: dict | None) -> None:
        color = border_color(envelope)
        if color is None:
            icon.configure(highlightthickness=0)
            return
        icon.configure(highlightthickness=4, highlightbackground=color, highlightcolor=color)

    def _score_text(self, part: Path) -> str:
        saved = self.scores.get(str(part.resolve()))
        if not isinstance(saved, dict) or "score" not in saved:
            return "No score yet"
        return format_score(saved)

    def _sync_run_button(self) -> None:
        state = "disabled" if self._busy or pywin32_status() is not None else "normal"
        self.run_button.configure(state=state)
        self.install_button.configure(state="disabled" if self._busy else "normal")

    def _set_busy(self, busy: bool, message: str) -> None:
        self._busy = busy
        self.progress.configure(text=message)
        self._sync_run_button()

    def run_selected(self) -> None:
        if self._busy or pywin32_status() is not None:
            return
        chosen = [row for row in self._rows if row["checked"].get()]
        if not chosen:
            messagebox.showinfo("Harness", "Select at least one part.")
            return
        self._set_busy(True, f"0 / {len(chosen)}")
        threading.Thread(target=self._run_worker, args=(chosen,), daemon=True).start()

    def _run_worker(self, rows: list[dict]) -> None:
        import pythoncom

        pythoncom.CoInitialize()
        try:
            import harness

            for index, row in enumerate(rows, start=1):
                path = row["path"]
                self.after(0, lambda i=index, n=len(rows), name=path.name: self.progress.configure(text=f"{i} / {n}  {name}"))
                try:
                    envelope = harness.grade_path(path)
                except Exception as exc:
                    self.after(0, lambda label=row["score"], err=str(exc): label.configure(text=err))
                    continue
                key = str(path.resolve())
                self.scores[key] = envelope
                save_scores(self.scores)
                shown = format_score(envelope)
                self.after(0, lambda label=row["score"], icon=row["icon"], text=shown, env=envelope: (
                    label.configure(text=text),
                    self._paint_border(icon, env),
                ))
        finally:
            pythoncom.CoUninitialize()
            self.after(0, lambda: self._set_busy(False, "Done"))


def main() -> None:
    Launcher().mainloop()


if __name__ == "__main__":
    main()
