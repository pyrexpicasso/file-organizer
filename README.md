# organizer.py
**A Python file organizer — development README & changelog**

Author: Jerome Jones

---

## Overview

organizer.py is a Python utility that automatically sorts files into structured folders based on extension, date, or custom rules. It supports dry-run previews, duplicate handling, logging, filtering, and a full tkinter GUI — built iteratively across three development phases.

---

## Quick Start

### CLI Usage

```bash
python organizer.py --source /path/to/folder --dry-run
python organizer.py --source /path/to/folder --dest /output
python organizer.py --gui
```

### Common Flags

| Flag | Description |
|------|-------------|
| `--source` | Source directory to organize |
| `--dest` | Destination root (defaults to source) |
| `--dry-run` | Preview operations without moving files |
| `--gui` | Launch the tkinter GUI |
| `--log` | Write operation log to timestamped file |
| `--report` | Print summary report after run |
| `--undo` | Reverse the last completed organize run |
| `--config` | Path to custom folder schema JSON |
| `--ext` | Filter: include only these extensions |
| `--exclude-ext` | Filter: skip these extensions |
| `--min-size / --max-size` | Filter by file size (bytes) |
| `--after / --before` | Filter by file date |

---

## Architecture

The script grew from a single flat file into a structured module by Phase 3:

| Module / File | Responsibility |
|---------------|----------------|
| `organizer.py` | Entry point — CLI parsing, orchestration |
| `core/mover.py` | File move logic, dry-run, cross-device support |
| `core/scanner.py` | Directory walk, filtering, duplicate detection (MD5) |
| `core/reporter.py` | Run summary, CSV export, log writing |
| `core/undo.py` | Move manifest storage and reversal logic |
| `gui/app.py` | tkinter GUI — tabs, progress bar, log panel, settings |
| `config/schema.json` | User-configurable folder routing rules |

---

## Development Timeline

### Phase 1 — Initial Build *(Early Development)*

#### Bugs Discovered
- Files moved even when `--dry-run` flag was active (flag not wired to move logic)
- Script crashed on permission-denied errors with no graceful fallback
- `os.rename()` failed silently when source and destination were on different drives
- No handling for files already existing at the destination (silent overwrites)

#### Bugs Fixed
- Dry-run flag properly gated all file operations — output now shows what would happen
- Added try/except around all file I/O with meaningful error messages
- Replaced `os.rename()` with `shutil.move()` for cross-device compatibility
- Added destination conflict detection before any file is touched

---

### Phase 2 — Feature Expansion *(Mid Development)*

#### Bugs Discovered
- Duplicate detection logic compared filenames only — missed byte-identical files with different names
- Date-based sorting used file modification time instead of creation time on some OS builds
- Extension mapping dict was case-sensitive — `.JPG` and `.jpg` routed to different folders
- Script hung indefinitely on very large directories with no progress feedback

#### Bugs Fixed
- Duplicate detection switched to MD5 hash comparison for content-level accuracy
- Date logic now checks creation time first, falls back to modification time gracefully
- All extensions normalized to lowercase before lookup — consistent routing regardless of case
- Added a file-count progress indicator so long runs give visible feedback

#### Features Added
- Filtering system — include/exclude files by extension, size range, or date range
- Duplicate file handler with user-configurable strategy (skip, rename, or overwrite)
- Logging system — all operations written to a timestamped `.log` file
- Reporting module — summary counts of files moved, skipped, and errored per run

#### Adjustments
- Folder structure schema made configurable via JSON instead of hardcoded dict
- CLI argument parser reorganized for clarity — grouped flags by function

---

### Phase 3 — GUI & Polish *(Late Development)*

#### Bugs Discovered
- GUI froze on large batch operations (file I/O running on the main thread)
- Progress bar reset to 0% when switching between tabs mid-operation
- Log panel in GUI did not auto-scroll — new entries appeared off-screen
- Undo feature reversed only the last file, not the full batch from a run

#### Bugs Fixed
- File operations offloaded to a background thread — GUI stays responsive throughout
- Progress state now persisted per-operation session, not per-tab
- Log panel patched with auto-scroll to bottom on each new entry
- Undo now stores the full move manifest per run and reverses all files in one action

#### Features Added
- Full GUI built with tkinter — drag-and-drop source/destination folder selection
- Undo/redo system — reverse any completed organize operation
- Preview mode in GUI — shows a tree of where every file will land before confirming
- Settings panel — save preferred folder schema and filter presets between sessions

#### Adjustments
- Script refactored from single-file to module structure (~900 lines across logical units)
- Report output format improved — human-readable summary + optional CSV export
- Default folder schema updated to better reflect common file type groupings

---

## Known Limitations & Next Steps

### Limitations
- No cloud storage support (Google Drive, Dropbox) — local filesystem only
- GUI not tested on Linux — primarily developed on Windows
- MD5 hashing slows significantly on very large files (>1GB) — no chunked streaming yet
- Config schema is JSON-only; no UI editor for it yet

### Planned
- Add chunked MD5 hashing for large file performance
- Unit test suite — especially for `mover.py` and `scanner.py` edge cases
- Plugin system for custom sort rules without editing `schema.json`
- Package as a standalone executable (PyInstaller)

---

