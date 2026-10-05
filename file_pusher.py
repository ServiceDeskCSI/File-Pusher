"""
File Pusher - copy a file to a list of Windows machines, one at a time.

Uses the administrative share (e.g. C:\\temp -> \\\\MACHINE\\C$\\temp), so the
account running this script needs admin rights on the target machines.
Requires only the Python standard library.
"""

import os
import re
import shutil
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


def to_unc(machine: str, dest: str) -> str:
    """Convert a local path like C:\\temp into \\\\machine\\C$\\temp."""
    m = re.match(r"^([A-Za-z]):[\\/]?(.*)$", dest.strip())
    if not m:
        raise ValueError("Destination must be a local path like C:\\temp")
    drive, rest = m.groups()
    rest = rest.replace("/", "\\").strip("\\")
    unc = f"\\\\{machine}\\{drive.upper()}$"
    return f"{unc}\\{rest}" if rest else unc


def smb_reachable(host: str, timeout: float = 3.0) -> bool:
    """Quick check on port 445 so offline machines fail fast instead of hanging."""
    try:
        with socket.create_connection((host, 445), timeout=timeout):
            return True
    except OSError:
        return False


def parse_machines(text: str) -> list[str]:
    """Split on newlines, commas, semicolons or spaces; drop blanks and duplicates."""
    seen, result = set(), []
    for name in re.split(r"[\s,;]+", text):
        name = name.strip().lstrip("\\")
        if name and name.lower() not in seen:
            seen.add(name.lower())
            result.append(name)
    return result


class FilePusherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("File Pusher")
        self.root.geometry("760x680")
        self.root.minsize(640, 560)

        self.msg_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.failed: list[tuple[str, str]] = []

        self._build_ui()
        self.root.after(100, self._process_queue)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)

        # File selection
        ttk.Label(main, text="File to copy:").grid(row=0, column=0, sticky="w", **pad)
        self.file_var = tk.StringVar()
        ttk.Entry(main, textvariable=self.file_var).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(main, text="Browse...", command=self._browse).grid(row=0, column=2, **pad)

        # Destination
        ttk.Label(main, text="Destination:").grid(row=1, column=0, sticky="w", **pad)
        self.dest_var = tk.StringVar(value=r"C:\temp")
        ttk.Entry(main, textvariable=self.dest_var).grid(row=1, column=1, sticky="ew", **pad)
        self.overwrite_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(main, text="Overwrite", variable=self.overwrite_var).grid(
            row=1, column=2, sticky="w", **pad)

        # Machine list
        ttk.Label(main, text="Machines (one per line):").grid(
            row=2, column=0, columnspan=3, sticky="w", **pad)
        self.machines_text = scrolledtext.ScrolledText(main, height=10, wrap="none")
        self.machines_text.grid(row=3, column=0, columnspan=3, sticky="nsew", **pad)
        main.rowconfigure(3, weight=1)

        # Buttons + progress
        btns = ttk.Frame(main)
        btns.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        btns.columnconfigure(2, weight=1)
        self.start_btn = ttk.Button(btns, text="Start Copy", command=self._start)
        self.start_btn.grid(row=0, column=0, padx=(0, 6))
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(0, 6))
        self.progress = ttk.Progressbar(btns, mode="determinate")
        self.progress.grid(row=0, column=2, sticky="ew")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(main, textvariable=self.status_var).grid(
            row=5, column=0, columnspan=3, sticky="w", **pad)

        # Log + failed list side by side
        bottom = ttk.Frame(main)
        bottom.grid(row=6, column=0, columnspan=3, sticky="nsew", **pad)
        main.rowconfigure(6, weight=1)
        bottom.columnconfigure(0, weight=3)
        bottom.columnconfigure(1, weight=2)
        bottom.rowconfigure(1, weight=1)

        ttk.Label(bottom, text="Log:").grid(row=0, column=0, sticky="w")
        self.log_text = scrolledtext.ScrolledText(bottom, height=10, state="disabled")
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=(0, 6))

        ttk.Label(bottom, text="Failed machines:").grid(row=0, column=1, sticky="w")
        self.failed_list = tk.Listbox(bottom, height=10)
        self.failed_list.grid(row=1, column=1, sticky="nsew")

        fbtns = ttk.Frame(bottom)
        fbtns.grid(row=2, column=1, sticky="e", pady=(4, 0))
        ttk.Button(fbtns, text="Copy Failed List", command=self._copy_failed).pack(side="left", padx=3)
        ttk.Button(fbtns, text="Retry Failed", command=self._retry_failed).pack(side="left")

    def _browse(self):
        path = filedialog.askopenfilename(title="Select file to copy")
        if path:
            self.file_var.set(os.path.normpath(path))

    def _log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- Actions ----------
    def _start(self):
        src = self.file_var.get().strip()
        dest = self.dest_var.get().strip()
        machines = parse_machines(self.machines_text.get("1.0", "end"))

        if not src or not os.path.isfile(src):
            messagebox.showerror("Error", "Please select a valid file.")
            return
        if not machines:
            messagebox.showerror("Error", "Please paste at least one machine name.")
            return
        try:
            to_unc("test", dest)
        except ValueError as e:
            messagebox.showerror("Error", str(e))
            return

        self.failed.clear()
        self.failed_list.delete(0, "end")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.progress.configure(maximum=len(machines), value=0)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.stop_event.clear()

        self.worker = threading.Thread(
            target=self._copy_worker,
            args=(src, dest, machines, self.overwrite_var.get()),
            daemon=True,
        )
        self.worker.start()

    def _stop(self):
        self.stop_event.set()
        self.status_var.set("Stopping after current machine...")

    def _copy_failed(self):
        if not self.failed:
            messagebox.showinfo("Failed machines", "No failed machines.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(m for m, _ in self.failed))
        self.status_var.set(f"Copied {len(self.failed)} failed machine(s) to clipboard.")

    def _retry_failed(self):
        if not self.failed:
            messagebox.showinfo("Retry", "No failed machines to retry.")
            return
        self.machines_text.delete("1.0", "end")
        self.machines_text.insert("1.0", "\n".join(m for m, _ in self.failed))
        self._start()

    # ---------- Worker thread ----------
    def _copy_worker(self, src, dest, machines, overwrite):
        filename = os.path.basename(src)
        ok = skipped = 0
        total = len(machines)

        for i, machine in enumerate(machines, 1):
            if self.stop_event.is_set():
                self.msg_queue.put(("log", "Stopped by user."))
                break

            self.msg_queue.put(("status", f"[{i}/{total}] Copying to {machine}..."))
            try:
                if not smb_reachable(machine):
                    raise ConnectionError("Unreachable (port 445 closed / offline)")

                target_dir = to_unc(machine, dest)
                os.makedirs(target_dir, exist_ok=True)
                target_file = os.path.join(target_dir, filename)

                if os.path.exists(target_file) and not overwrite:
                    skipped += 1
                    self.msg_queue.put(("log", f"SKIP  {machine}: file already exists"))
                else:
                    shutil.copy2(src, target_file)
                    ok += 1
                    self.msg_queue.put(("log", f"OK    {machine} -> {target_file}"))
            except Exception as e:
                reason = str(e) or e.__class__.__name__
                self.msg_queue.put(("failed", (machine, reason)))
                self.msg_queue.put(("log", f"FAIL  {machine}: {reason}"))

            self.msg_queue.put(("progress", i))

        self.msg_queue.put(("done", (ok, skipped, total)))

    def _process_queue(self):
        try:
            while True:
                kind, data = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self.status_var.set(data)
                elif kind == "progress":
                    self.progress.configure(value=data)
                elif kind == "failed":
                    self.failed.append(data)
                    self.failed_list.insert("end", data[0])
                elif kind == "done":
                    ok, skipped, total = data
                    summary = (f"Done. Success: {ok}  Skipped: {skipped}  "
                               f"Failed: {len(self.failed)}  (of {total})")
                    self.status_var.set(summary)
                    self._log("-" * 50 + "\n" + summary)
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._process_queue)


if __name__ == "__main__":
    root = tk.Tk()
    FilePusherApp(root)
    root.mainloop()