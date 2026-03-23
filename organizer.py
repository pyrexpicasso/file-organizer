import os
import shutil
import json
import hashlib
import argparse
import logging
from datetime import datetime

# Optional: watchdog for watch mode
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object

# Optional: tkinter for GUI
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

# ---------------------------------------------------------------------------
# Default category map
# ---------------------------------------------------------------------------
DEFAULT_FILE_TYPES = {
    "Images":    [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp"],
    "Videos":    [".mp4", ".mov", ".avi", ".mkv", ".wmv"],
    "Documents": [".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".pptx"],
    "Music":     [".mp3", ".wav", ".aac", ".flac"],
    "Archives":  [".zip", ".tar", ".gz", ".rar", ".7z"],
    "Code":      [".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp"],
}

HISTORY_FILE = ".organize_history.json"
LOG_FILE     = "organize_log.txt"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(config_path):
    """Load a custom category map from a JSON file, or fall back to defaults."""
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            return json.load(f)
    return DEFAULT_FILE_TYPES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_file_hash(filepath):
    """Return the MD5 hash of a file (used for duplicate detection)."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_timestamped_name(filename):
    """Prefix a filename with today's date, e.g. 2026-03-22_photo.jpg"""
    timestamp = datetime.now().strftime("%Y-%m-%d_")
    name, ext = os.path.splitext(filename)
    return f"{timestamp}{name}{ext}"


def setup_logger(folder_path):
    """Set up a file logger that writes to organize_log.txt in the target folder."""
    log_path = os.path.join(folder_path, LOG_FILE)
    logger = logging.getLogger("organizer")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(log_path)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# Undo support
# ---------------------------------------------------------------------------
def save_history(moves, folder_path):
    """Append a session's moves to the hidden history file for undo support."""
    history_path = os.path.join(folder_path, HISTORY_FILE)
    history = {"sessions": []}
    if os.path.exists(history_path):
        with open(history_path) as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                pass
    history["sessions"].append({
        "timestamp": datetime.now().isoformat(),
        "moves": moves,
    })
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)


def undo_last(folder_path):
    """Reverse the most recent organize session."""
    history_path = os.path.join(folder_path, HISTORY_FILE)
    if not os.path.exists(history_path):
        print("No history found. Nothing to undo.")
        return
    with open(history_path) as f:
        try:
            history = json.load(f)
        except json.JSONDecodeError:
            print("History file is corrupted.")
            return
    if not history.get("sessions"):
        print("No sessions to undo.")
        return

    last_session = history["sessions"].pop()
    restored = 0
    for move in reversed(last_session["moves"]):
        src, dest = move["src"], move["dest"]
        if os.path.exists(dest):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dest, src)
            print(f"Restored '{os.path.basename(src)}'")
            restored += 1
        else:
            print(f"Skipping '{os.path.basename(src)}' — file not found at destination")

    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)
    print(f"\nUndo complete. Restored {restored} file(s).")


# ---------------------------------------------------------------------------
# Core organizer
# ---------------------------------------------------------------------------
def organize_folder(folder_path, file_types, dry_run=False, recursive=False,
                    rename=False, stats=None, moves_log=None, logger=None):
    """Organize files in folder_path into category subfolders."""
    if stats is None:
        stats = {}
    if moves_log is None:
        moves_log = []

    # Folders managed by the organizer — skip these during recursion
    managed = set(file_types.keys()) | {"Other", "Duplicates"}

    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)

        if os.path.isdir(file_path):
            if recursive and filename not in managed:
                organize_folder(file_path, file_types, dry_run, recursive,
                                rename, stats, moves_log, logger)
            continue

        # Skip hidden / system files
        if filename.startswith("."):
            continue

        dest_filename = get_timestamped_name(filename) if rename else filename
        _, ext = os.path.splitext(filename)
        ext = ext.lower()

        # Determine category
        category = "Other"
        for cat, extensions in file_types.items():
            if ext in extensions:
                category = cat
                break

        dest_folder = os.path.join(folder_path, category)
        dest_path   = os.path.join(dest_folder, dest_filename)

        # Duplicate / conflict handling
        if os.path.exists(dest_path):
            src_hash  = get_file_hash(file_path)
            dest_hash = get_file_hash(dest_path)
            if src_hash == dest_hash:
                # Identical file — route to Duplicates/
                dest_folder   = os.path.join(folder_path, "Duplicates")
                dest_path     = os.path.join(dest_folder, dest_filename)
                category      = "Duplicates"
            else:
                # Same name, different content — add _conflict suffix
                base, file_ext = os.path.splitext(dest_filename)
                dest_filename  = f"{base}_conflict{file_ext}"
                dest_path      = os.path.join(dest_folder, dest_filename)

        prefix = "[DRY RUN] " if dry_run else ""
        msg = f"{prefix}Moved '{filename}' -> {category}/{dest_filename}"
        print(msg)
        if logger:
            logger.info(msg)

        stats[category] = stats.get(category, 0) + 1

        if not dry_run:
            os.makedirs(dest_folder, exist_ok=True)
            shutil.move(file_path, dest_path)
            moves_log.append({"src": file_path, "dest": dest_path})

    return stats, moves_log


# ---------------------------------------------------------------------------
# Stats summary
# ---------------------------------------------------------------------------
def print_stats(stats):
    if not stats:
        print("\nNo files were moved.")
        return
    print("\n--- Summary ---")
    for category, count in sorted(stats.items()):
        print(f"  {category:<15} {count} file{'s' if count != 1 else ''}")
    total = sum(stats.values())
    print(f"  {'Total':<15} {total} file{'s' if total != 1 else ''}")


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------
class FolderHandler(FileSystemEventHandler):
    def __init__(self, folder_path, file_types, rename, logger):
        self.folder_path = folder_path
        self.file_types  = file_types
        self.rename      = rename
        self.logger      = logger

    def on_created(self, event):
        if not event.is_directory:
            import time
            time.sleep(0.5)  # wait for the file to finish writing
            filename = os.path.basename(event.src_path)
            if not filename.startswith("."):
                stats, moves_log = organize_folder(
                    self.folder_path, self.file_types,
                    rename=self.rename, logger=self.logger
                )
                if moves_log:
                    save_history(moves_log, self.folder_path)
                print_stats(stats)


def watch_folder(folder_path, file_types, rename=False, logger=None):
    if not WATCHDOG_AVAILABLE:
        print("watchdog is not installed. Run: pip3 install watchdog")
        return
    handler  = FolderHandler(folder_path, file_types, rename, logger)
    observer = Observer()
    observer.schedule(handler, folder_path, recursive=False)
    observer.start()
    print(f"Watching '{folder_path}' for new files... Press Ctrl+C to stop.")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def launch_gui(file_types):
    if not TKINTER_AVAILABLE:
        print("tkinter is not available on this system.")
        return

    root = tk.Tk()
    root.title("File Organizer")
    root.geometry("520x460")
    root.resizable(False, False)

    # Folder picker
    tk.Label(root, text="Folder to organize:", anchor="w").pack(fill="x", padx=12, pady=(12, 0))
    row = tk.Frame(root)
    row.pack(fill="x", padx=12)
    folder_var = tk.StringVar()
    tk.Entry(row, textvariable=folder_var, width=44).pack(side="left", fill="x", expand=True)

    def browse():
        path = filedialog.askdirectory()
        if path:
            folder_var.set(path)

    tk.Button(row, text="Browse", command=browse).pack(side="right", padx=(4, 0))

    # Option checkboxes
    tk.Label(root, text="Options:", anchor="w").pack(fill="x", padx=12, pady=(10, 0))
    dry_run_var   = tk.BooleanVar()
    recursive_var = tk.BooleanVar()
    rename_var    = tk.BooleanVar()
    log_var       = tk.BooleanVar()
    tk.Checkbutton(root, text="Dry run (preview only, no files moved)",    variable=dry_run_var).pack(anchor="w", padx=24)
    tk.Checkbutton(root, text="Recursive (include subfolders)",            variable=recursive_var).pack(anchor="w", padx=24)
    tk.Checkbutton(root, text="Rename files with date prefix",             variable=rename_var).pack(anchor="w", padx=24)
    tk.Checkbutton(root, text="Save log to organize_log.txt",              variable=log_var).pack(anchor="w", padx=24)

    # Output area
    tk.Label(root, text="Output:", anchor="w").pack(fill="x", padx=12, pady=(8, 0))
    output = tk.Text(root, height=10, state="disabled",
                     bg="#1e1e1e", fg="#d4d4d4", font=("Courier", 10))
    output.pack(fill="both", expand=True, padx=12, pady=(0, 6))

    def run():
        folder = os.path.expanduser(folder_var.get().strip())
        folder = os.path.abspath(folder)
        if not os.path.exists(folder):
            messagebox.showerror("Error", f"Folder not found:\n{folder}")
            return

        output.config(state="normal")
        output.delete("1.0", tk.END)

        import sys

        class Redirect:
            def write(self, text):
                output.insert(tk.END, text)
                output.see(tk.END)
                root.update()
            def flush(self):
                pass

        old_stdout = sys.stdout
        sys.stdout = Redirect()

        logger = setup_logger(folder) if log_var.get() else None
        stats, moves_log = organize_folder(
            folder, file_types,
            dry_run=dry_run_var.get(),
            recursive=recursive_var.get(),
            rename=rename_var.get(),
            logger=logger,
        )
        if not dry_run_var.get() and moves_log:
            save_history(moves_log, folder)
        print_stats(stats)
        print("Done!")

        sys.stdout = old_stdout
        output.config(state="disabled")

    tk.Button(root, text="Organize", command=run,
              bg="#4CAF50", fg="white", font=("Helvetica", 11, "bold"), pady=4
              ).pack(pady=6)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="File Organizer — sort files into folders by type"
    )
    parser.add_argument("folder",      nargs="?",        help="Path to the folder to organize")
    parser.add_argument("--dry-run",   action="store_true", help="Preview moves without changing anything")
    parser.add_argument("--undo",      action="store_true", help="Undo the last organize session")
    parser.add_argument("--recursive", action="store_true", help="Organize subfolders recursively")
    parser.add_argument("--rename",    action="store_true", help="Prefix filenames with today's date")
    parser.add_argument("--watch",     action="store_true", help="Watch folder and auto-organize new files")
    parser.add_argument("--gui",       action="store_true", help="Launch the graphical interface")
    parser.add_argument("--config",    metavar="FILE",      help="Path to a custom categories config.json")
    parser.add_argument("--log",       action="store_true", help="Write a log file (organize_log.txt)")
    args = parser.parse_args()

    file_types = load_config(args.config)

    if args.gui:
        launch_gui(file_types)
        return

    if args.folder:
        folder = os.path.expanduser(args.folder)
        folder = os.path.abspath(folder)
    else:
        folder = input("Enter the path to the folder you want to organize: ").strip()
        folder = os.path.expanduser(folder)
        folder = os.path.abspath(folder)

    if not os.path.exists(folder):
        print(f"Folder not found: {folder}")
        return

    if args.undo:
        undo_last(folder)
        return

    logger = setup_logger(folder) if args.log else None

    if args.watch:
        watch_folder(folder, file_types, rename=args.rename, logger=logger)
        return

    stats, moves_log = organize_folder(
        folder, file_types,
        dry_run=args.dry_run,
        recursive=args.recursive,
        rename=args.rename,
        logger=logger,
    )

    if not args.dry_run and moves_log:
        save_history(moves_log, folder)

    print_stats(stats)
    print("Done!")


if __name__ == "__main__":
    main()
