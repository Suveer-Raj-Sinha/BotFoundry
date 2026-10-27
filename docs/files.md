# File nodes (Phase 3.1, read-only)

Status: engine 0.10.0. Code: `engine/botfoundry/files/`. **Nothing here changes a file.** Moving, planning and
undo come in later Phase 3 steps.

## Nodes
| Node | Needs | In | Out | What it does |
|---|---|---|---|---|
| `files.scan_folder` | `fs.read` | `start` (optional) | `items`, `skipped`, `stats` | Lists the files in an approved folder |
| `files.filter` | nothing | `items` | `matched`, `rejected`, `stats` | Splits records by extension, name, size or age |
| `files.hash` | `fs.read` | `items` | `items`, `failed`, `stats` | Reads files and adds a content hash (SHA-256) |
| `files.find_duplicates` | nothing | `items` | `groups`, `duplicates`, `stats` | Groups identical files and suggests which to keep |

Only scanning and hashing touch the disk, and only through the engine: the Rulebook must grant `fs.read`, the
folder must be approved, and every path is checked (and resolved) before it is read. Filter and Find Duplicates
work on the records only, so they need no permission.

## The file record
```json
{"path": "C:\\Users\\Me\\Downloads\\report.PDF", "name": "report.PDF", "stem": "report", "extension": ".pdf",
 "size": 48210, "modified": "2026-01-02T03:04:05Z"}
```
`files.hash` adds `hash` and `hash_algorithm`. `hash: null` means the file was deliberately not read because
its size is unique (only with `skip_unique_sizes`); a record with **no** `hash` key was never hashed, and
Find Duplicates refuses it (`duplicates.not_hashed`) instead of answering "no duplicates".

## What the scanner will not do
- **Follow links.** Symlinks, junctions, mount points and (on Windows) any reparse point are listed under
  `skipped` and never entered or read. This includes **OneDrive "files on demand" placeholders**: reading one
  would silently download it.
- **List hidden or system entries** unless `include_hidden` is set (names starting with `.`, or the Windows
  hidden/system attribute). They are counted in `stats.hidden_skipped`.
- **Enter forbidden folders.** A subfolder the Rulebook forbids is skipped whole and reported with the reason.
- **Hide problems.** An unreadable subfolder or file is reported under `skipped` and the scan continues. A
  missing or unreadable *starting* folder fails the node (`scan.folder_not_found`, `scan.folder_unreadable`).
- **Cut results short.** More than `max_items` files (default 100,000) fails with `scan.too_many_files`.
Output order is deterministic (sorted by path).

## What hashing guarantees
- Every path is re-authorized before it is read, so a workflow that forges records cannot make the node read a
  file outside the approved folders (`path.outside_allowed`).
- A file that changed since the scan (different size) or **while it was read** is not trusted: it is listed in
  `failed` with the reason. A vanished, locked or permission-denied file is listed too. Failures never abort the
  whole node and are never guessed at.
- `skip_unique_sizes` reads only files whose size matches another file's, which makes duplicate finding far
  faster. A different size means different content, so those files cannot have a duplicate.

## Filter settings
`extensions` (with or without the dot, any case), `name_contains`, `name_regex` (case-insensitive),
`min_size` / `max_size` (bytes), `older_than_days` / `newer_than_days` (by modified time; files with an unknown
age never match), `match` (`all` or `any`), `negate`, and `reference_time` to fix "now" for repeatable results.
At least one criterion is required, and bad values are rejected before the workflow runs.

## Find Duplicates
Identical content means same size and same hash. `keep` suggests one copy per group: `oldest` (default),
`newest`, `shortest_path` or `first`; ties are broken by shorter path, then name, so the answer does not depend
on input order. Empty files are ignored unless `ignore_empty` is false. It only reports: `duplicates` is the flat
list of redundant copies, ready for a later step to act on (with approval).

## Windows notes
- Long paths: paths of 240+ characters are opened with the `\\?\` prefix, so they work without the system
  long-path setting. A Windows-only test creates a path over 260 characters.
- Folder and file names are compared case-insensitively by the path rules; 8.3 short names (`PROGRA~1`) are
  refused by the path layer, so use full folder names in Rulebooks.
- Files open in another program may be unreadable; they appear in `failed`.

## Command line
```
python -m botfoundry duplicates FOLDER [--no-recursive] [--include-hidden] [--min-size BYTES]
                                       [--keep oldest|newest|shortest_path|first] [--top N] [--json]
```
Builds a temporary Rulebook that approves only that folder and runs scan, hash and duplicates through the real
engine, so the safety rules apply: drive roots, your whole profile folder and protected system locations are
refused. It prints what was skipped or unreadable as well as the duplicate groups, and ends with
"Read-only: nothing was moved, changed or deleted."

To run the same thing as a workflow: edit `examples/find-duplicates.workflow.json` and
`examples/downloads-cleaner.rulebook.json` (replace `YourName`), then
`python -m botfoundry run examples\find-duplicates.workflow.json --rulebook examples\downloads-cleaner.rulebook.json --no-save`.

## Limits worth knowing
- Hard links to the same data are reported as duplicates (the scanner cannot tell them apart on Windows
  without extra calls), so "wasted bytes" can be overstated for them.
- Paths are checked at one moment; a file can be swapped later. Steps that change files must re-check
  immediately before acting (Phase 3.3).
- Everything is held in memory during a run (about 250 bytes per file): 100,000 files is fine, millions are not.
  Large results are stored in the database as a preview only (see `docs/persistence.md`); the run itself keeps
  and passes on the full list.
