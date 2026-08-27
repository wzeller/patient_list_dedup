#!/usr/bin/env python3
"""Minimal Tkinter GUI for patient_list_dedup.

Choose a CSV, optionally adjust the fuzzy-name threshold and cluster grouping,
then write the deduplicated CSV. All processing happens locally — no data ever
leaves the machine.

Run from source with:  python3 gui.py
"""
from __future__ import annotations

import io
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# In a windowed (bundled) app, stdout/stderr can be None; the library prints
# warnings there. Give them harmless sinks so those prints never crash the app.
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

import patient_list_dedup as pld  # noqa: E402


class DedupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.input_path: Path | None = None
        root.title("Patient List Dedup")
        root.minsize(540, 300)
        self._build()

    def _build(self) -> None:
        frm = ttk.Frame(self.root, padding=16)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Patient List Deduplication", font=("", 16, "bold")).pack(anchor="w")
        ttk.Label(
            frm,
            text="Flags likely-duplicate patients in a CSV and recommends which "
            "account to keep. All processing happens on this Mac — nothing is uploaded.",
            wraplength=500,
            foreground="#555",
        ).pack(anchor="w", pady=(2, 12))

        file_row = ttk.Frame(frm)
        file_row.pack(fill="x")
        ttk.Button(file_row, text="Choose spreadsheet…", command=self.choose_file).pack(side="left")
        self.file_label = ttk.Label(file_row, text="No file selected", foreground="#888")
        self.file_label.pack(side="left", padx=10)

        opts = ttk.LabelFrame(frm, text="Options", padding=10)
        opts.pack(fill="x", pady=12)

        thr_row = ttk.Frame(opts)
        thr_row.pack(fill="x", pady=2)
        ttk.Label(thr_row, text="Fuzzy name-match threshold (0–1):").pack(side="left")
        self.threshold = tk.StringVar(value=str(pld.DEFAULT_NAME_THRESHOLD))
        ttk.Entry(thr_row, textvariable=self.threshold, width=6).pack(side="left", padx=8)

        self.group = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="Group duplicate clusters together in the output", variable=self.group
        ).pack(anchor="w", pady=2)

        self.run_btn = ttk.Button(frm, text="Run deduplication", command=self.run)
        self.run_btn.pack(pady=6)

        self.status = ttk.Label(frm, text="", wraplength=500, foreground="#333")
        self.status.pack(anchor="w", pady=(6, 0))

    def choose_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a patient list CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if path:
            self.input_path = Path(path)
            self.file_label.config(text=self.input_path.name, foreground="#000")
            self.status.config(text="")

    def run(self) -> None:
        if not self.input_path:
            messagebox.showwarning("No file", "Please choose a spreadsheet first.")
            return
        try:
            threshold = float(self.threshold.get())
            if not 0.0 <= threshold <= 1.0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid threshold", "Threshold must be a number between 0 and 1.")
            return

        out = filedialog.asksaveasfilename(
            title="Save deduplicated CSV as…",
            defaultextension=".csv",
            initialfile=f"{self.input_path.stem}_dedup.csv",
            initialdir=str(self.input_path.parent),
            filetypes=[("CSV files", "*.csv")],
        )
        if not out:
            return

        self.run_btn.config(state="disabled")
        self.status.config(text="Working…", foreground="#333")
        self.root.update_idletasks()
        try:
            yes = pld.process(
                self.input_path,
                Path(out),
                None,
                threshold,
                pld.NAME_ALIASES,
                pld.DOB_ALIASES,
                pld.MRN_ALIASES,
                group_sort=self.group.get(),
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the user
            self.status.config(text="")
            messagebox.showerror("Could not process file", str(exc))
            self.run_btn.config(state="normal")
            return

        self.run_btn.config(state="normal")
        self.status.config(
            text=f"Done. Flagged {yes} patient(s) as likely duplicates.\nSaved to {out}",
            foreground="#1a7f37",
        )
        messagebox.showinfo(
            "Finished", f"Flagged {yes} likely-duplicate patient(s).\n\nSaved to:\n{out}"
        )


def main() -> None:
    root = tk.Tk()
    DedupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
