# File nodes (Phase 3.1 and 3.2)

Status: engine 0.11.0. Code: `engine/botfoundry/files/`. **Nothing here changes a file.** Moving, the approval
gate, the recovery log and undo come in later Phase 3 steps.

## Nodes
| Node | Needs | In | Out | What it does |
|---|---|---|---|---|
| `files.scan_folder` | `fs.read` | `start` (optional) | `items`, `skipped`, `stats` | Lists the files in an approved folder |
| `files.filter` | nothing | `items` | `matched`, `rejected`, `stats` | Splits records by extension, name, size or age |
| `files.hash` | `fs.read` | `items` | `items`, `failed`, `stats` | Reads files and adds a content hash (SHA-256) |
| `files.find_duplicates` | nothing | `items` | `groups`, `duplicates`, `stats` | Groups identical files and suggests which to keep |
| `files.plan_moves` | nothing (it only *asks* about `fs.move`) | `items` | `plan`, `skipped`, `stats` | Decides where each file would go; moves nothing |

Scanning and hashing read the disk, and Plan Moves only checks whether a destination is free (never file contents, and
only for destinations the Rulebook has just allowed). All of it goes through the engine: the Rulebook must grant `fs.read`, the
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

## Plan Moves (Phase 3.2)
Turns file records into a **plan**: a list of moves to review. It changes nothing.

### Settings
| Setting | Meaning |
|---|---|
| `destination_root` | Absolute folder the files are sorted into. Must be approved by the Rulebook; need not exist yet |
| `rules` | Your own rules, first match wins. Each has `folder` (relative, e.g. `Work/Invoices`), an optional `label`, and the same tests as Filter Items (`extensions`, `name_contains`, `name_regex`, sizes, ages, `match`, `negate`) |
| `use_default_rules` | After your rules, sort by type: Images, Documents, Spreadsheets, Presentations, Archives, Audio, Video, Installers (default on) |
| `unmatched_folder` | Folder for files no rule matches. Empty: they stay where they are (default) |
| `date_folders` | `none`, `year` (`2026`) or `year_month` (`2026-11`), from the modified date in UTC |
| `reference_time` | Fixes "now" for age tests |

Folder names must be relative and Windows-safe (no `..`, no reserved names like `NUL`, no `<>:"|?*`, no trailing dot
or space, at most 100 characters per level and 8 levels). A bad name is a settings error, not thousands of skips.
Unknown settings are errors too, so a typo cannot silently change what happens.

### What it guarantees
- **Never overwrites.** A destination that already exists is skipped (`destination.exists`).
- **No arbitrary winners.** When two files want the same destination, **neither** moves (`destination.collision`),
  and each skip names the others. Names that differ only in case collide on Windows.
- **A file in the way** of a category folder blocks only that folder (`destination.folder_blocked`).
- **Already sorted is recognised.** A file already at its destination is left alone (`already_in_place`), so
  running again after a sort is calm. A file sitting in the wrong category folder is planned to move.
- **The Rulebook is asked about every source and every destination.** A refusal skips that file with the Rulebook's
  code (`path.forbidden`, `path.outside_allowed`, `path.escapes_via_link`, ...), so a plan never contains a move the
  Rulebook would refuse. If the Rulebook does not allow `fs.move` at all, or the destination folder itself is
  refused, the node fails with that reason (`There is nothing to plan: ...`) instead of returning a pile of skips.
- **Records are not trusted.** The name and type come from the path, not from the record's fields.
- **Deterministic.** Same files and settings give the same plan whatever the input order. `plan_id` is a SHA-256
  over every move (source, destination, size, modified time, hash if present) and the destination folder: tie any
  approval to it.
- **No silent cuts.** More moves than the Rulebook's `max_files_per_run` leaves the plan whole and adds a warning.

### Planning does not need approval; moving will
Plan Moves declares no permission. It uses `ctx.preflight` to ask what the Rulebook would say, so a Rulebook that
sets `fs.move` to `ask` does **not** pause the run for a plan. `plan.approval_required` is true in that case: it is
the later move step that will ask, with the plan in front of the person.

### The plan
```json
{"version": 1, "plan_id": "<64 hex>", "destination_root": "C:\\Users\\Me\\Downloads\\Organized",
 "moves": [{"source": "C:\\Users\\Me\\Downloads\\a.JPG", "destination": "C:\\Users\\Me\\Downloads\\Organized\\Images\\a.JPG",
            "name": "a.JPG", "size": 48210, "modified": "2026-01-02T03:04:05Z", "category": "Images",
            "rule": "default: Images"}],
 "folders_to_create": ["C:\\Users\\Me\\Downloads\\Organized\\Images"], "approval_required": true, "warnings": []}
```
`hash` and `hash_algorithm` appear on a move only when the record had them (put File Hash before Plan Moves). Without
a hash the plan still works; a later move step must hash a file before moving it. `folders_to_create` lists
destination folders that do not exist yet (the immediate parent of each move).
`skipped` is a list of `{path, code, reason}`; `stats` has `files_considered`, `moves`, `skipped`, `bytes_to_move`,
`folders_to_create`, `by_category` and `skipped_by_reason`.

Skip codes: `rule.no_match`, `already_in_place`, `date.unknown`, `destination.exists`, `destination.collision`,
`destination.folder_blocked`, `destination.invalid`, `source.invalid`, and any Rulebook path code.
Settings errors: `plan.bad_destination`, `plan.bad_folder`, `plan.preflight_failed`.

### Command line (preview only)
```
python -m botfoundry organize FOLDER [--to DEST] [--rules FILE] [--no-default-rules] [--unmatched-folder NAME]
                                     [--date-folders none|year|year_month] [--no-recursive] [--include-hidden]
                                     [--top N] [--json]
```
Sorts into `FOLDER\Organized` unless `--to` says otherwise. It builds a temporary Rulebook that approves only the
folder (and `--to` when it is elsewhere), allows reading and sets moving to *ask*, then runs scan and plan through
the real engine, so drive roots, your whole profile and protected locations are refused. `--rules` takes a JSON
file with `rules`, `use_default_rules`, `unmatched_folder` and `date_folders`. It ends with
"Preview only: nothing was moved, changed or deleted. This version cannot move files yet."

As a workflow: edit `examples/organize-downloads.workflow.json` and `examples/downloads-cleaner.rulebook.json`
(replace `YourName`), then `python -m botfoundry run examples\\organize-downloads.workflow.json --rulebook
examples\\downloads-cleaner.rulebook.json --no-save`.

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
- Paths are checked at one moment; a file can be swapped later. A plan is a proposal made at one moment: steps that
  change files must re-check every path, and that the destination is still free, immediately before acting (Phase 3.3).
- Moves are planned between whatever drives the paths are on; the safe cross-drive method (copy, verify the hash,
  then remove the source) belongs to the move step (Phase 3.3), not the plan.
- Everything is held in memory during a run (about 250 bytes per file): 100,000 files is fine, millions are not.
  Large results are stored in the database as a preview only (see `docs/persistence.md`); the run itself keeps
  and passes on the full list.
