import os
import sys
import time
import shutil
import json
import re
import hashlib
import argparse
import logging
import fnmatch
import csv
import html as html_module
from datetime import datetime, timedelta

# Optional: watchdog for watch mode
try:
    from watchdog.observers import Observer  # type: ignore
    from watchdog.events import FileSystemEventHandler  # type: ignore
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object

# Optional: tkinter for GUI
try:
    import tkinter as tk  # type: ignore
    from tkinter import filedialog, messagebox  # type: ignore
    TKINTER_AVAILABLE = True
except ImportError:
    TKINTER_AVAILABLE = False

# Optional: tqdm for progress bars
try:
    from tqdm import tqdm  # type: ignore
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Extension definitions
# ---------------------------------------------------------------------------
SUSPICIOUS_EXTENSIONS = {
    "Executable":    [".exe", ".com", ".scr", ".pif"],
    "Script":        [".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse",
                      ".ps1", ".psm1", ".psd1", ".ws", ".wsh", ".wsf", ".hta"],
    "System/Driver": [".dll", ".sys", ".drv"],
    "Installer":     [".msi", ".msp"],
    "Registry":      [".reg"],
    "Java":          [".jar"],
    "MacroDoc":      [".xlsm", ".docm", ".pptm", ".xlam"],
    "DiskImage":     [".iso", ".img"],
}

DEFAULT_FILE_TYPES = {
    "Images":    [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".svg", ".webp"],
    "Videos":    [".mp4", ".mov", ".avi", ".mkv", ".wmv"],
    "Documents": [".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".pptx"],
    "Music":     [".mp3", ".wav", ".aac", ".flac"],
    "Archives":  [".zip", ".tar", ".gz", ".rar", ".7z"],
    "Code":      [".py", ".js", ".ts", ".html", ".css", ".java", ".c", ".cpp"],
}

HISTORY_FILE  = ".organize_history.json"
LOG_FILE      = "organize_log.txt"
HTML_REPORT   = "organize_report.html"
CSV_REPORT    = "organize_report.csv"

# Files the organizer itself creates — never touch these
INTERNAL_FILES = {LOG_FILE, HISTORY_FILE, HTML_REPORT, CSV_REPORT}

DEFAULT_IGNORE_PATTERNS = [
    "thumbs.db", "desktop.ini", ".DS_Store", "*.tmp", "*.temp", "~*"
]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config(config_path):
    if config_path and os.path.exists(config_path):
        with open(config_path) as f:
            return json.load(f)
    return DEFAULT_FILE_TYPES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_file_hash(filepath):
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def get_timestamped_name(filename):
    timestamp = datetime.now().strftime("%Y-%m-%d_")
    name, ext = os.path.splitext(filename)
    return f"{timestamp}{name}{ext}"


# Matches date prefixes added by --rename, e.g. "2026-04-02_photo.jpg"
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")

def strip_date_prefix(filename):
    """Remove a YYYY-MM-DD_ prefix if present, returning the base name."""
    return _DATE_PREFIX_RE.sub("", filename)


def get_date_subfolder(filepath):
    """Return YYYY/MonthName based on file mtime."""
    dt = datetime.fromtimestamp(os.path.getmtime(filepath))
    return os.path.join(str(dt.year), dt.strftime("%B"))


def human_size(nbytes):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def setup_logger(folder_path):
    log_path    = os.path.join(folder_path, LOG_FILE)
    logger_name = f"organizer_{os.path.abspath(folder_path)}"
    logger      = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.FileHandler(log_path)
        h.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
        logger.addHandler(h)
    return logger


def matches_ignore(filename, patterns):
    lower = filename.lower()
    return any(fnmatch.fnmatch(lower, p.lower()) for p in patterns)


def passes_filters(file_path, min_size=None, max_size=None,
                   older_than=None, newer_than=None):
    try:
        stat = os.stat(file_path)
    except OSError:
        return False
    size  = stat.st_size
    mtime = datetime.fromtimestamp(stat.st_mtime)
    if min_size   is not None and size  < min_size:    return False
    if max_size   is not None and size  > max_size:    return False
    if older_than is not None and mtime >= older_than: return False
    if newer_than is not None and mtime <= newer_than: return False
    return True


def apply_custom_rules(filename, custom_rules):
    """Return a category from keyword rules, or None."""
    lower = filename.lower()
    for rule in (custom_rules or []):
        kw = rule.get("contains", "").lower()
        if kw and kw in lower:
            return rule.get("category")
    return None


def verify_copy(src, dest):
    try:
        return get_file_hash(src) == get_file_hash(dest)
    except Exception:
        return False


def describe_duplicate_pair(src_path, dest_path):
    """Print side-by-side info about two files."""
    def info(p):
        s = os.stat(p)
        return human_size(s.st_size), datetime.fromtimestamp(s.st_mtime).strftime("%Y-%m-%d %H:%M")
    ss, sm = info(src_path)
    ds, dm = info(dest_path)
    print(f"    Incoming : {os.path.basename(src_path):<40}  {ss:>10}  {sm}")
    print(f"    Existing : {os.path.basename(dest_path):<40}  {ds:>10}  {dm}")


def find_existing_in_category(base_category_folder, filename):
    """
    Return the path of `filename` if it exists anywhere under base_category_folder,
    or None. Handles flat layout (Images/photo.jpg) and date-nested layout
    (Images/2024/January/photo.jpg) transparently.
    """
    # Fast path: flat layout
    flat = os.path.join(base_category_folder, filename)
    if os.path.exists(flat):
        return flat
    # Walk date subfolders
    if os.path.isdir(base_category_folder):
        for root, _dirs, files in os.walk(base_category_folder):
            if filename in files:
                return os.path.join(root, filename)
    return None


def resolve_duplicate_auto(src_path, dest_path, keep="newest"):
    src_t  = os.path.getmtime(src_path)
    dest_t = os.path.getmtime(dest_path)
    if keep == "newest":
        return "keep_src" if src_t > dest_t else "keep_dest"
    return "keep_src" if src_t < dest_t else "keep_dest"


# ---------------------------------------------------------------------------
# Undo support
# ---------------------------------------------------------------------------
def save_history(moves, folder_path):
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
        if not os.path.exists(dest):
            print(f"Skipping '{os.path.basename(src)}' — file not found at destination")
            continue
        try:
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dest, src)
            print(f"Restored '{os.path.basename(src)}'")
            restored += 1
        except Exception as exc:
            print(f"  ERROR restoring '{os.path.basename(src)}': {exc}")

    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)
    print(f"\nUndo complete. Restored {restored} file(s).")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_html_report(moves_log, stats, folder_path, dry_run=False, errors=0):
    report_path = os.path.join(folder_path, HTML_REPORT)
    mode = "DRY RUN PREVIEW" if dry_run else "COMPLETED"
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rows = "".join(
        f"<tr><td>{html_module.escape(os.path.basename(m['src']))}</td>"
        f"<td>{html_module.escape(os.path.relpath(m['dest'], folder_path))}</td></tr>\n"
        for m in moves_log
    )
    stat_rows = "".join(
        f"<tr><td>{html_module.escape(cat)}</td><td>{count}</td></tr>\n"
        for cat, count in sorted(stats.items())
    )
    if errors:
        stat_rows += f'<tr style="color:#c62828"><td>Errors</td><td>{errors}</td></tr>\n'
    total = sum(stats.values())

    move_label = "Previewed Files (Dry Run)" if dry_run else "File Moves"
    col_label  = "Would Move To" if dry_run else "Destination"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>File Organizer Report</title>
<style>
  body{{font-family:Arial,sans-serif;margin:32px;color:#222}}
  h1{{color:#2e7d32}} h2{{color:#555;margin-top:28px}}
  table{{border-collapse:collapse;width:100%;max-width:860px}}
  th,td{{border:1px solid #ddd;padding:8px 12px;text-align:left}}
  th{{background:#f5f5f5}} tr:nth-child(even){{background:#fafafa}}
  .badge{{display:inline-block;padding:2px 10px;border-radius:12px;
    background:{'#fff3e0' if dry_run else '#e8f5e9'};
    color:{'#e65100' if dry_run else '#2e7d32'};font-weight:bold;font-size:.9em}}
  .note{{font-style:italic;color:#888;font-size:.9em}}
</style></head>
<body>
<h1>File Organizer Report</h1>
<p>Generated: {ts} &nbsp;<span class="badge">{mode}</span></p>
<p>Folder: <code>{html_module.escape(folder_path)}</code></p>
{'<p class="note">This is a dry-run preview. No files were actually moved.</p>' if dry_run else ''}
<h2>Summary by Category {'(Preview)' if dry_run else ''}</h2>
<table><tr><th>Category</th><th>{'Would Move' if dry_run else 'Files Moved'}</th></tr>
{stat_rows}<tr><th>Total</th><th>{total}</th></tr></table>
<h2>{move_label} ({len(moves_log)})</h2>
<table><tr><th>File</th><th>{col_label}</th></tr>
{rows if rows else '<tr><td colspan="2">No files moved.</td></tr>'}
</table></body></html>"""

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  HTML report -> {report_path}")


def generate_csv_report(moves_log, stats, folder_path, dry_run=False, errors=0):
    report_path = os.path.join(folder_path, CSV_REPORT)
    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type", "key", "value"])
        w.writerow(["meta", "generated", datetime.now().isoformat()])
        w.writerow(["meta", "dry_run",   str(dry_run)])
        w.writerow(["meta", "folder",    folder_path])
        for cat, count in sorted(stats.items()):
            w.writerow(["stat", cat, count])
        w.writerow(["stat", "Total", sum(stats.values())])
        if errors:
            w.writerow(["stat", "Errors", errors])
        for m in moves_log:
            w.writerow(["move", m["src"], m["dest"]])
    print(f"  CSV  report -> {report_path}")


# ---------------------------------------------------------------------------
# Stats-only scan
# ---------------------------------------------------------------------------
def stats_only(folder_path, file_types, recursive=False, ignore_patterns=None,
               min_size=None, max_size=None, older_than=None, newer_than=None):
    ignore_patterns = ignore_patterns or DEFAULT_IGNORE_PATTERNS
    stats   = {}
    managed = set(file_types.keys()) | {"Other", "Duplicates"}

    def _scan(dirpath):
        try:
            entries = os.listdir(dirpath)
        except PermissionError:
            return
        for name in entries:
            full = os.path.join(dirpath, name)
            if os.path.isdir(full):
                if recursive and name not in managed:
                    _scan(full)
                continue
            if name.startswith(".") or name in INTERNAL_FILES:
                continue
            if matches_ignore(name, ignore_patterns):
                continue
            if not passes_filters(full, min_size, max_size, older_than, newer_than):
                continue
            _, ext = os.path.splitext(name)
            cat = "Other"
            for c, exts in file_types.items():
                if ext.lower() in exts:
                    cat = c
                    break
            if cat not in stats:
                stats[cat] = {"count": 0, "bytes": 0}
            stats[cat]["count"] += 1
            stats[cat]["bytes"] += os.path.getsize(full)

    _scan(folder_path)
    return stats


def print_stats_only(stats):
    if not stats:
        print("\nNo files found.")
        return
    print("\n--- File Count by Type (no files moved) ---")
    tc = tb = 0
    for cat, data in sorted(stats.items()):
        c, b = data["count"], data["bytes"]
        print(f"  {cat:<15} {c:>5} file{'s' if c != 1 else ''}   {human_size(b):>10}")
        tc += c; tb += b
    print(f"  {'Total':<15} {tc:>5} files       {human_size(tb):>10}")


# ---------------------------------------------------------------------------
# Core organizer
# ---------------------------------------------------------------------------
def organize_folder(folder_path, file_types, dry_run=False, recursive=False,
                    rename=False, by_date=False, copy_mode=False,
                    interactive=False, verify=False, keep_duplicate="route",
                    ignore_patterns=None, custom_rules=None,
                    min_size=None, max_size=None, older_than=None, newer_than=None,
                    stats=None, moves_log=None, errors=None, logger=None,
                    target_file=None, progress=None):
    if stats        is None: stats        = {}
    if moves_log    is None: moves_log    = []
    if errors       is None: errors       = [0]   # mutable counter shared across recursion
    if ignore_patterns is None: ignore_patterns = DEFAULT_IGNORE_PATTERNS

    managed = set(file_types.keys()) | {"Other", "Duplicates"}

    try:
        filenames = [target_file] if target_file else os.listdir(folder_path)
    except PermissionError:
        print(f"  Permission denied: {folder_path}")
        return stats, moves_log, errors

    for filename in filenames:
        file_path = os.path.join(folder_path, filename)

        if os.path.isdir(file_path):
            if recursive and filename not in managed:
                organize_folder(
                    file_path, file_types, dry_run, recursive, rename,
                    by_date, copy_mode, interactive, verify, keep_duplicate,
                    ignore_patterns, custom_rules, min_size, max_size,
                    older_than, newer_than, stats, moves_log, errors, logger,
                    progress=progress,
                )
            continue

        # Bug 3 fix: never touch files the organizer itself creates
        if filename in INTERNAL_FILES:
            continue
        # Skip the organizer script itself to avoid moving it
        if os.path.abspath(file_path) == os.path.abspath(__file__):
            continue
        if filename.startswith(".") or filename in (LOG_FILE, HISTORY_FILE):
            continue
        if matches_ignore(filename, ignore_patterns):
            continue
        if not passes_filters(file_path, min_size, max_size, older_than, newer_than):
            continue

        _, ext = os.path.splitext(filename)
        ext = ext.lower()

        # Category: custom rules first, then extension map
        category = apply_custom_rules(filename, custom_rules)
        if category is None:
            category = "Other"
            for cat, extensions in file_types.items():
                if ext in extensions:
                    category = cat
                    break

        dest_folder      = os.path.join(folder_path, category)
        base_dest_folder = dest_folder
        if by_date:
            dest_folder = os.path.join(dest_folder, get_date_subfolder(file_path))

        dest_filename = get_timestamped_name(filename) if rename else filename
        dest_path     = os.path.join(dest_folder, dest_filename)

        # Bug 2 fix: strip date prefix so re-runs detect the already-renamed file
        check_name = strip_date_prefix(filename)

        # Bug 1 fix: search the whole category tree, not just the flat base folder
        existing_path = find_existing_in_category(base_dest_folder, check_name)

        _file_to_delete = None

        if existing_path:
            src_hash  = get_file_hash(file_path)
            dest_hash = get_file_hash(existing_path)
            if src_hash == dest_hash:
                if keep_duplicate == "route":
                    dest_folder   = os.path.join(folder_path, "Duplicates")
                    dest_path     = os.path.join(dest_folder, dest_filename)
                    category      = "Duplicates"
                elif keep_duplicate in ("newest", "oldest"):
                    decision = resolve_duplicate_auto(file_path, existing_path, keep_duplicate)
                    if decision == "keep_dest":
                        print(f"  Skipping duplicate (keeping existing): {filename}")
                        if progress: progress.update(1)
                        continue
                    else:
                        _file_to_delete = existing_path
                elif keep_duplicate == "ask":
                    print(f"\n  Duplicate: {filename}")
                    describe_duplicate_pair(file_path, existing_path)
                    choice = input("  Keep [i]ncoming / [e]xisting / [r]oute to Duplicates? ").strip().lower()
                    if choice == "e":
                        if progress: progress.update(1)
                        continue
                    elif choice == "r":
                        dest_folder = os.path.join(folder_path, "Duplicates")
                        dest_path   = os.path.join(dest_folder, dest_filename)
                        category    = "Duplicates"
            else:
                # Same name, different content — conflict rename
                base, file_ext = os.path.splitext(dest_filename)
                counter = 1
                candidate = f"{base}_conflict_{counter}{file_ext}"
                while os.path.exists(os.path.join(dest_folder, candidate)):
                    counter  += 1
                    candidate = f"{base}_conflict_{counter}{file_ext}"
                dest_filename = candidate
                dest_path     = os.path.join(dest_folder, dest_filename)

        # Interactive confirmation
        if interactive and not dry_run:
            verb = "Copy" if copy_mode else "Move"
            ans  = input(f"  {verb} '{filename}' -> {category}/{dest_filename}? [y/N] ").strip().lower()
            if ans != "y":
                if progress: progress.update(1)
                continue

        if dry_run:
            verb = "copy" if copy_mode else "move"
            msg  = f"[DRY RUN] Would {verb} '{filename}' -> {category}/{dest_filename}"
            print(msg)
            if logger: logger.info(msg)
            stats[category] = stats.get(category, 0) + 1
        else:
            try:
                os.makedirs(dest_folder, exist_ok=True)
                if copy_mode:
                    src_hash_before = get_file_hash(file_path) if verify else None
                    shutil.copy2(file_path, dest_path)
                    if verify and get_file_hash(dest_path) != src_hash_before:
                        print(f"  WARNING: Checksum mismatch after copy of '{filename}'!")
                        if logger: logger.warning(f"Checksum mismatch: {filename}")
                else:
                    src_hash_before = get_file_hash(file_path) if verify else None
                    shutil.move(file_path, dest_path)
                    if verify and (not os.path.exists(dest_path) or
                                   get_file_hash(dest_path) != src_hash_before):
                        print(f"  WARNING: Verification failed after move of '{filename}'!")
                        if logger: logger.warning(f"Verification failed: {filename}")
                    if _file_to_delete and os.path.exists(_file_to_delete):
                        os.remove(_file_to_delete)

                verb = "Copied" if copy_mode else "Moved"
                msg  = f"{verb} '{filename}' -> {category}/{dest_filename}"
                print(msg)
                if logger: logger.info(msg)
                stats[category] = stats.get(category, 0) + 1
                if not copy_mode:
                    moves_log.append({"src": file_path, "dest": dest_path})

            except Exception as exc:
                # Bug 4 fix: track errors so summary can report them
                errors[0] += 1
                print(f"  ERROR on '{filename}': {exc}")
                if logger: logger.error(f"Failed on '{filename}': {exc}")

        if progress: progress.update(1)

    return stats, moves_log, errors


# ---------------------------------------------------------------------------
# Suspicious file scanner
# ---------------------------------------------------------------------------
def scan_suspicious(folder_path, recursive=False):
    ext_to_category = {
        ext: cat
        for cat, exts in SUSPICIOUS_EXTENSIONS.items()
        for ext in exts
    }
    results = []

    def _scan_dir(dirpath):
        try:
            entries = os.listdir(dirpath)
        except PermissionError:
            return
        for name in entries:
            full = os.path.join(dirpath, name)
            if os.path.isdir(full):
                if recursive: _scan_dir(full)
            else:
                if name.startswith(".") or name in INTERNAL_FILES:
                    continue
                ext_lower = os.path.splitext(name)[1].lower()
                if ext_lower in ext_to_category:
                    results.append({
                        "path": full, "filename": name,
                        "extension": ext_lower,
                        "category": ext_to_category[ext_lower],
                    })

    _scan_dir(folder_path)
    return results


def print_scan_results(results, folder_path):
    if not results:
        print("No suspicious files found.")
        return
    print(f"\n--- Suspicious Files Found: {len(results)} ---")
    by_category = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r)
    for category, items in sorted(by_category.items()):
        print(f"\n  [{category}]")
        for item in items:
            print(f"    {os.path.relpath(item['path'], folder_path)}")
    print()


# ---------------------------------------------------------------------------
# Stats summary
# ---------------------------------------------------------------------------
def print_stats(stats, dry_run=False, errors=0):
    if not stats:
        label = "previewed" if dry_run else "moved"
        print(f"\nNo files were {label}.")
        if errors:
            print(f"  Errors: {errors}")
        return
    label = "Would move" if dry_run else "Moved"
    print(f"\n--- Summary ({label}) ---")
    for category, count in sorted(stats.items()):
        print(f"  {category:<15} {count} file{'s' if count != 1 else ''}")
    total = sum(stats.values())
    print(f"  {'Total':<15} {total} file{'s' if total != 1 else ''}")
    if errors:
        print(f"  {'Errors':<15} {errors} file{'s' if errors != 1 else ''} failed")


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------
class FolderHandler(FileSystemEventHandler):
    def __init__(self, folder_path, file_types, rename, logger,
                 ignore_patterns=None, custom_rules=None):
        self.folder_path     = folder_path
        self.file_types      = file_types
        self.rename          = rename
        self.logger          = logger
        self.ignore_patterns = ignore_patterns or DEFAULT_IGNORE_PATTERNS
        self.custom_rules    = custom_rules or []

    def on_created(self, event):
        if not event.is_directory:
            path = event.src_path
            prev_size = -1
            for _ in range(10):
                try:
                    curr_size = os.path.getsize(path)
                except OSError:
                    curr_size = -1
                if curr_size == prev_size and curr_size >= 0:
                    break
                prev_size = curr_size
                time.sleep(0.3)
            filename = os.path.basename(path)
            if not filename.startswith("."):
                stats, moves_log, errors = organize_folder(
                    self.folder_path, self.file_types,
                    rename=self.rename, logger=self.logger,
                    ignore_patterns=self.ignore_patterns,
                    custom_rules=self.custom_rules,
                    target_file=filename,
                )
                if moves_log:
                    save_history(moves_log, self.folder_path)
                print_stats(stats)


def watch_folder(folder_path, file_types, rename=False, logger=None,
                 ignore_patterns=None, custom_rules=None):
    if not WATCHDOG_AVAILABLE:
        print("watchdog is not installed. Run: pip3 install watchdog")
        return
    handler  = FolderHandler(folder_path, file_types, rename, logger,
                             ignore_patterns, custom_rules)
    observer = Observer()
    observer.schedule(handler, folder_path, recursive=False)
    observer.start()
    print(f"Watching '{folder_path}' for new files... Press Ctrl+C to stop.")
    try:
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
    root.geometry("680x780")
    root.resizable(False, False)

    # --- Folder row ---
    tk.Label(root, text="Folder to organize:", anchor="w").pack(fill="x", padx=12, pady=(12, 0))
    row = tk.Frame(root); row.pack(fill="x", padx=12)
    folder_var = tk.StringVar()
    tk.Entry(row, textvariable=folder_var, width=55).pack(side="left", fill="x", expand=True)

    def browse():
        path = filedialog.askdirectory()
        if path: folder_var.set(path)
    tk.Button(row, text="Browse", command=browse).pack(side="right", padx=(4, 0))

    # --- Checkboxes ---
    tk.Label(root, text="Options:", anchor="w").pack(fill="x", padx=12, pady=(10, 0))
    dry_run_var        = tk.BooleanVar()
    recursive_var      = tk.BooleanVar()
    rename_var         = tk.BooleanVar()
    by_date_var        = tk.BooleanVar()
    copy_mode_var      = tk.BooleanVar()
    verify_var         = tk.BooleanVar()
    interactive_var    = tk.BooleanVar()
    log_var            = tk.BooleanVar()
    html_report_var    = tk.BooleanVar()
    csv_report_var     = tk.BooleanVar()
    scan_recursive_var = tk.BooleanVar()
    progress_var       = tk.BooleanVar()

    for label, var in [
        ("Dry run (preview only)",                       dry_run_var),
        ("Recursive (include subfolders)",               recursive_var),
        ("Rename files with date prefix",                rename_var),
        ("Organise into date subfolders (YYYY/Month/)",  by_date_var),
        ("Copy files instead of moving",                 copy_mode_var),
        ("Verify copies with checksum",                  verify_var),
        ("Interactive — confirm each file",              interactive_var),
        ("Show progress bar (requires tqdm)",            progress_var),
        ("Save log to organize_log.txt",                 log_var),
        ("Generate HTML report",                         html_report_var),
        ("Generate CSV report",                          csv_report_var),
        ("Scan subfolders for suspicious files",         scan_recursive_var),
    ]:
        tk.Checkbutton(root, text=label, variable=var).pack(anchor="w", padx=24)

    # --- Duplicate handling ---
    dup_frame = tk.Frame(root); dup_frame.pack(fill="x", padx=12, pady=(6, 0))
    tk.Label(dup_frame, text="Duplicate handling:").pack(side="left")
    keep_dup_var = tk.StringVar(value="route")
    for val, lbl in [("route", "Route to Duplicates/"), ("newest", "Keep newest"),
                     ("oldest", "Keep oldest"), ("ask", "Ask each time")]:
        tk.Radiobutton(dup_frame, text=lbl, variable=keep_dup_var, value=val).pack(side="left", padx=4)

    # --- Filters ---
    tk.Label(root, text="Filters (leave blank to skip):", anchor="w").pack(fill="x", padx=12, pady=(8, 0))
    filter_frame = tk.Frame(root); filter_frame.pack(fill="x", padx=24)
    min_size_var  = tk.StringVar()
    max_size_var  = tk.StringVar()
    older_var     = tk.StringVar()
    newer_var     = tk.StringVar()
    ignore_var    = tk.StringVar()
    for lbl, var, hint in [
        ("Min size",        min_size_var,  "e.g. 500KB"),
        ("Max size",        max_size_var,  "e.g. 1GB"),
        ("Older than days", older_var,     "e.g. 30"),
        ("Newer than days", newer_var,     "e.g. 7"),
        ("Ignore patterns", ignore_var,    "e.g. *.tmp ~*"),
    ]:
        r = tk.Frame(filter_frame); r.pack(fill="x", pady=1)
        tk.Label(r, text=f"{lbl}:", width=16, anchor="w").pack(side="left")
        e = tk.Entry(r, textvariable=var, width=24); e.pack(side="left")
        tk.Label(r, text=hint, fg="#888").pack(side="left", padx=4)

    # --- Custom rules ---
    tk.Label(root, text="Custom rules (JSON):", anchor="w").pack(fill="x", padx=12, pady=(6, 0))
    rules_var = tk.StringVar()
    tk.Entry(root, textvariable=rules_var, width=60).pack(fill="x", padx=24)
    tk.Label(root, text='e.g. [{"contains":"invoice","category":"Finance"}]',
             fg="#888", font=("Helvetica", 9)).pack(anchor="w", padx=24)

    # --- Output ---
    tk.Label(root, text="Output:", anchor="w").pack(fill="x", padx=12, pady=(8, 0))
    output = tk.Text(root, height=8, state="disabled",
                     bg="#1e1e1e", fg="#d4d4d4", font=("Courier", 10))
    output.pack(fill="both", expand=True, padx=12, pady=(0, 6))

    class Redirect:
        def write(self, text):
            output.insert(tk.END, text)
            output.see(tk.END)
            root.update()
        def flush(self): pass

    def _parse_opt_size(s):
        s = s.strip()
        return parse_size(s) if s else None

    def _parse_opt_int(s):
        s = s.strip()
        return int(s) if s else None

    def run():
        folder = os.path.abspath(os.path.expanduser(folder_var.get().strip()))
        if not os.path.exists(folder):
            messagebox.showerror("Error", f"Folder not found:\n{folder}"); return

        output.config(state="normal"); output.delete("1.0", tk.END)
        old_stdout = sys.stdout; sys.stdout = Redirect()

        try:
            min_size   = _parse_opt_size(min_size_var.get())
            max_size   = _parse_opt_size(max_size_var.get())
            older_days = _parse_opt_int(older_var.get())
            newer_days = _parse_opt_int(newer_var.get())
            older_than = datetime.now() - timedelta(days=older_days) if older_days else None
            newer_than = datetime.now() - timedelta(days=newer_days) if newer_days else None

            ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)
            raw_ignore = ignore_var.get().strip()
            if raw_ignore:
                ignore_patterns.extend(raw_ignore.split())

            custom_rules = []
            raw_rules = rules_var.get().strip()
            if raw_rules:
                try:
                    custom_rules = json.loads(raw_rules)
                except json.JSONDecodeError as e:
                    messagebox.showerror("Error", f"Invalid rules JSON:\n{e}")
                    sys.stdout = old_stdout; output.config(state="disabled"); return

            progress = None
            if progress_var.get():
                if TQDM_AVAILABLE:
                    progress = tqdm(total=None, unit="file", desc="Organizing")
                else:
                    print("tqdm not installed — continuing without progress bar.")

            logger = setup_logger(folder) if log_var.get() else None
            dry_run = dry_run_var.get()

            stats, moves_log, errors = organize_folder(
                folder, file_types,
                dry_run=dry_run, recursive=recursive_var.get(),
                rename=rename_var.get(), by_date=by_date_var.get(),
                copy_mode=copy_mode_var.get(), verify=verify_var.get(),
                interactive=interactive_var.get(),
                keep_duplicate=keep_dup_var.get(),
                ignore_patterns=ignore_patterns,
                custom_rules=custom_rules,
                min_size=min_size, max_size=max_size,
                older_than=older_than, newer_than=newer_than,
                logger=logger, progress=progress,
            )
            if progress: progress.close()
            if not dry_run and moves_log:
                save_history(moves_log, folder)
            if html_report_var.get():
                generate_html_report(moves_log, stats, folder, dry_run=dry_run, errors=errors[0])
            if csv_report_var.get():
                generate_csv_report(moves_log, stats, folder, dry_run=dry_run, errors=errors[0])
            print_stats(stats, dry_run=dry_run, errors=errors[0])
            print("Done!")
        finally:
            sys.stdout = old_stdout
            output.config(state="disabled")

    def run_scan():
        folder = os.path.abspath(os.path.expanduser(folder_var.get().strip()))
        if not os.path.exists(folder):
            messagebox.showerror("Error", f"Folder not found:\n{folder}"); return
        output.config(state="normal"); output.delete("1.0", tk.END)
        old_stdout = sys.stdout; sys.stdout = Redirect()
        try:
            results = scan_suspicious(folder, recursive=scan_recursive_var.get())
            print_scan_results(results, folder)
        finally:
            sys.stdout = old_stdout
            output.config(state="disabled")

    btn_row = tk.Frame(root); btn_row.pack(pady=6)
    tk.Button(btn_row, text="Organize",        command=run,
              bg="#4CAF50", fg="white", font=("Helvetica", 11, "bold"), pady=4
              ).pack(side="left", padx=4)
    tk.Button(btn_row, text="Scan Suspicious", command=run_scan,
              bg="#e65100", fg="white", font=("Helvetica", 11, "bold"), pady=4
              ).pack(side="left", padx=4)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def parse_size(s):
    """Parse size strings like '500KB', '2MB', '1GB' into bytes."""
    s = s.strip().upper()
    for unit, mult in [("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("KB", 1024), ("B", 1)]:
        if s.endswith(unit):
            return int(float(s[:-len(unit)]) * mult)
    return int(s)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="File Organizer — sort files into folders by type",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python organizer.py ~/Downloads --dry-run
  python organizer.py ~/Downloads --recursive --by-date
  python organizer.py ~/Downloads --copy --verify --html-report
  python organizer.py ~/Downloads --min-size 1MB --older-than 30
  python organizer.py ~/Downloads --ignore "*.tmp" "~*" --stats-only
  python organizer.py ~/Downloads --keep-duplicate newest
  python organizer.py ~/Downloads --interactive
  python organizer.py ~/Downloads --rules '[{"contains":"invoice","category":"Finance"}]'
""",
    )

    parser.add_argument("folder",      nargs="?",       help="Path to the folder to organize")

    # Modes
    parser.add_argument("--dry-run",       action="store_true", help="Preview without moving anything")
    parser.add_argument("--undo",          action="store_true", help="Undo the last organize session")
    parser.add_argument("--watch",         action="store_true", help="Watch folder and auto-organize new files")
    parser.add_argument("--gui",           action="store_true", help="Launch the graphical interface")
    parser.add_argument("--stats-only",    action="store_true", help="Count files by type without moving")
    parser.add_argument("--scan",          action="store_true", help="Scan for suspicious file extensions")
    parser.add_argument("--scan-recursive",action="store_true", help="Scan subfolders for suspicious files")

    # Organisation
    parser.add_argument("--recursive",    action="store_true", help="Organize subfolders recursively")
    parser.add_argument("--rename",       action="store_true", help="Prefix filenames with today's date")
    parser.add_argument("--by-date",      action="store_true", help="Organize into Category/YYYY/Month/")
    parser.add_argument("--copy",         action="store_true", help="Copy files instead of moving")
    parser.add_argument("--verify",       action="store_true", help="Checksum-verify each copy/move")
    parser.add_argument("--interactive",  action="store_true", help="Confirm each file move individually")
    parser.add_argument("--config",       metavar="FILE",      help="Path to custom categories JSON")
    parser.add_argument("--keep-duplicate", default="route",
                        choices=["route", "newest", "oldest", "ask"],
                        help="How to handle identical duplicates (default: route)")

    # Filters
    parser.add_argument("--min-size",    metavar="SIZE", help="Skip files smaller than SIZE (e.g. 500KB)")
    parser.add_argument("--max-size",    metavar="SIZE", help="Skip files larger than SIZE (e.g. 1GB)")
    parser.add_argument("--older-than",  metavar="DAYS", type=int, help="Only files older than N days")
    parser.add_argument("--newer-than",  metavar="DAYS", type=int, help="Only files newer than N days")
    parser.add_argument("--ignore",      metavar="PATTERN", nargs="+",
                        help="Glob patterns to skip, added to defaults (e.g. '*.tmp' '~*')")

    # Output
    parser.add_argument("--log",         action="store_true", help="Write organize_log.txt")
    parser.add_argument("--html-report", action="store_true", help="Generate HTML summary report")
    parser.add_argument("--csv-report",  action="store_true", help="Generate CSV summary report")
    parser.add_argument("--progress",    action="store_true", help="Show progress bar (requires tqdm)")

    # Custom rules
    parser.add_argument("--rules", metavar="JSON",
                        help='Keyword rules as JSON: \'[{"contains":"invoice","category":"Finance"}]\'')

    args = parser.parse_args()
    file_types = load_config(args.config)

    if args.gui:
        launch_gui(file_types)
        return

    if args.folder:
        folder = os.path.abspath(os.path.expanduser(args.folder))
    else:
        folder = os.path.abspath(os.path.expanduser(
            input("Enter the path to the folder you want to organize: ").strip()
        ))

    if not os.path.exists(folder):
        print(f"Folder not found: {folder}")
        return

    if args.undo:
        undo_last(folder)
        return

    if args.scan or args.scan_recursive:
        results = scan_suspicious(folder, recursive=args.scan_recursive)
        print_scan_results(results, folder)
        return

    # Parse filters
    min_size   = parse_size(args.min_size)                         if args.min_size   else None
    max_size   = parse_size(args.max_size)                         if args.max_size   else None
    older_than = datetime.now() - timedelta(days=args.older_than)  if args.older_than else None
    newer_than = datetime.now() - timedelta(days=args.newer_than)  if args.newer_than else None

    ignore_patterns = list(DEFAULT_IGNORE_PATTERNS)
    if args.ignore:
        ignore_patterns.extend(args.ignore)

    custom_rules = []
    if args.rules:
        try:
            custom_rules = json.loads(args.rules)
        except json.JSONDecodeError as e:
            print(f"Invalid --rules JSON: {e}"); return

    if args.interactive and args.dry_run:
        print("Note: --interactive has no effect in --dry-run mode. Proceeding with dry run.")

    if args.stats_only:
        s = stats_only(folder, file_types, recursive=args.recursive,
                       ignore_patterns=ignore_patterns,
                       min_size=min_size, max_size=max_size,
                       older_than=older_than, newer_than=newer_than)
        print_stats_only(s)
        return

    logger = setup_logger(folder) if args.log else None

    if args.watch:
        watch_folder(folder, file_types, rename=args.rename, logger=logger,
                     ignore_patterns=ignore_patterns, custom_rules=custom_rules)
        return

    # Optional progress bar — indeterminate mode so filters don't cause mismatch
    progress = None
    if args.progress:
        if TQDM_AVAILABLE:
            progress = tqdm(total=None, unit="file", desc="Organizing")
        else:
            print("tqdm not installed. Run: pip3 install tqdm  (continuing without progress bar)")

    stats, moves_log, errors = organize_folder(
        folder, file_types,
        dry_run=args.dry_run,
        recursive=args.recursive,
        rename=args.rename,
        by_date=args.by_date,
        copy_mode=args.copy,
        interactive=args.interactive,
        verify=args.verify,
        keep_duplicate=args.keep_duplicate,
        ignore_patterns=ignore_patterns,
        custom_rules=custom_rules,
        min_size=min_size,
        max_size=max_size,
        older_than=older_than,
        newer_than=newer_than,
        logger=logger,
        progress=progress,
    )

    if progress:
        progress.close()

    if not args.dry_run and not args.copy and moves_log:
        save_history(moves_log, folder)

    if args.html_report:
        generate_html_report(moves_log, stats, folder, dry_run=args.dry_run, errors=errors[0])
    if args.csv_report:
        generate_csv_report(moves_log, stats, folder, dry_run=args.dry_run, errors=errors[0])

    print_stats(stats, dry_run=args.dry_run, errors=errors[0])
    print("Done!")


if __name__ == "__main__":
    main()
