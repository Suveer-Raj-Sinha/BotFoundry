# Git and GitHub workflow

## What is (and is not) committed
Commit source, configuration, lockfiles and docs. Never commit secrets, databases, logs, build output,
installed dependencies or the frozen engine binary. The `.gitignore` enforces this; check with
`git status` before every commit.

| Ignored | Why |
|---|---|
| `engine/.venv/`, `node_modules/`, `target/`, `dist/` | Recreated by installing / building |
| `engine/build/`, `apps/desktop/src-tauri/binaries/` | PyInstaller output (about 15 MB, rebuilt by `scripts/build_engine.py`) |
| `*.db`, `*.db-wal`, `*.db-shm`, `engine/.data/`, `*.log` | User data and logs |
| `.env`, `.env.*`, `*.local` | Local config and tokens (`.env.example` is allowed) |
| `.vscode/`, `.idea/`, OS files | Personal editor settings |

Lockfiles (`package-lock.json`, `Cargo.lock`) ARE committed so builds are reproducible.

## One-time setup before the first push
```powershell
git config user.name  "Your Name"
git config user.email "the-email-linked-to-your-GitHub-account"   # commits only count on your profile if this matches
git branch -M main                                                 # rename master to main
```
Check that nothing that should be ignored was committed earlier (nothing should be printed):
```powershell
git ls-files | Select-String -Pattern "node_modules|\.venv|engine/build|src-tauri/binaries|src-tauri/target|__pycache__|\.db$|\.local$|\.log$"
```
If something is listed: `git rm -r --cached <path>`, then commit. If a large binary is already in history and
you have NOT pushed yet, rewrite the history before pushing (ask for help; do not push first).

Create an empty repository on GitHub (no README, no .gitignore, no license; you already have them), then:
```powershell
git remote add origin https://github.com/<your-username>/botfoundry.git
git push -u origin main
```
Choose a license before making the repository public. Without one, others have no right to use the code.
MIT is a common permissive choice; add it as `LICENSE`.

## Commit rules
- **One logical change per commit**, and every commit should leave the tests passing.
- Run before committing: `pytest engine -q`, `npm test` (in `apps/desktop`), `scripts\bump.py check`.
- Format: `type(scope): short summary in the imperative`, for example
  `feat(storage): add SQLite database with migrations`.

| Type | Use for |
|---|---|
| `feat` | New capability |
| `fix` | Bug fix |
| `test` | Tests only |
| `docs` | Documentation, changelog, handoff |
| `build` | Build scripts, packaging, dependencies |
| `chore` | Housekeeping (.gitignore, versions) |
| `refactor` | Restructure without changing behavior |

- Stage deliberately: `git add <paths>` or `git add -p`. Avoid `git add .` unless you checked `git status`.
- Many small meaningful commits make a better history and are easier to review. Do not create empty or
  padding commits; reviewers can tell, and history becomes noisy.

## Branches and pull requests
- `main` always works. Do each piece of work on a branch: `git switch -c phase-1-3-storage`.
- Open a pull request into `main` even when working alone; it gives you a reviewable record.
- When merging on GitHub choose **"Create a merge commit"** or **"Rebase and merge"**. "Squash and merge"
  collapses your commits into one and throws the granular history away.

## Versions and tags
Engine and app have separate versions (see `scripts/bump.py` and the two CHANGELOG files). After a milestone:
```powershell
git tag -a engine-v0.6.0 -m "Engine 0.6.0: SQLite persistence"
git tag -a app-v0.5.0 -m "App 0.5.0: passes the data directory to the engine"
git push origin --tags
```

## Per-step checklist
1. Branch: `git switch -c <phase-name>`.
2. Build the step; update the changelog(s) and run `bump.py engine|app X.Y.Z` and `bump.py check`.
3. Tests pass.
4. Commit in logical pieces (the assistant provides a commit plan with each step).
5. Push the branch, open a pull request, merge, then `git switch main` and `git pull`.
6. Tag the milestone.
