# DZLL Codex Instructions

## Project

DZLL — DayZ Linux Launcher.

## Hard Rules

- Do not use mutating Git commands except for the narrowly scoped housekeeping
  exception below.
- `git rm --cached`, `git add`, and `git commit` are permitted only when the user
  explicitly authorizes those operations for a specific task. Limit them to the
  paths and commit content authorized for that task; do not stage unrelated
  files.
- All other Git operations remain read-only for inspection/status/diff only:
  - `git status`
  - `git diff`
  - `git log`
  - `git show`
  - `git grep`
  - `git ls-files`
- Use `git --no-pager` for Git output.
- Do not push or force push, tag, reset, checkout/switch branches, rebase, merge,
  stash, clean, amend, rewrite history, or otherwise modify Git state/history
  outside the explicitly authorized housekeeping exception above.
- Do not edit files outside this project unless explicitly asked.
- Do not use `xdg-open` for Steam URLs.
- Do not add Flatpak Steam support. Flatpak Steam is unsupported.
- Native Steam only for the Steam Client backend.
- The Steam Client backend uses Steam UGC only.
- SteamCMD has been removed and is no longer a supported backend or fallback.
- Do not reintroduce SteamCMD.
- Do not reintroduce `steam +workshop_download_item`.
- Final reports must be written under:
  `devReports/`
- Do not print or repeat report contents in terminal/chat output.
- Terminal/chat output after a completed task must contain only:
  - a short completion confirmation
  - the full path to the report file

## Project Memory

Project memory contains historical context and established decisions, but it may
become stale.

When memory conflicts with current source code, configuration, tests, or an
explicit user instruction, do not silently assume memory is correct.
Investigate the discrepancy and prefer current authoritative evidence.

At the start of a new Codex session for this repository, read `DZLL_MEMORY.md` once
to load durable project context.

Do not reread `DZLL_MEMORY.md` during the same session while its contents are still
available in context.

Reread it only if:
- the session/context has been compacted or lost
- the user explicitly asks for it
- another process may have changed the file
- you need to verify an exact memory entry before modifying it

Use `DZLL_MEMORY.md` for:
- prior architectural decisions
- known traps
- confirmed root causes
- non-obvious invariants
- approaches investigated and ruled out
- other durable project knowledge

Current source code, tests, configuration, and explicit user instructions are
more authoritative than memory when they conflict.

Update `DZLL_MEMORY.md` only when:
- durable project knowledge was learned that would materially help a future
  Codex session
- an existing memory entry became stale, incorrect, or misleading

Include:
- architectural facts discovered
- decisions made and why
- non-obvious invariants
- confirmed bugs and root causes
- approaches explicitly investigated and ruled out

Do not add:
- transient debugging output
- information obvious from the current code
- speculation or unconfirmed conclusions
- task-specific chatter
- temporary state unlikely to matter later

Keep project memory concise, accurate, and useful.
Revise or remove obsolete entries rather than treating it as an append-only log.

Audits are allowed to update `DZLL_MEMORY.md` when they discover durable project
knowledge that satisfies the rules above.

## Current Architecture

- Server browser: Column View only.
- Steam Client backend: native Steam using Steam UGC.
- Flatpak Steam is unsupported.
- SteamCMD has been removed and must not be reintroduced.
- `steam +workshop_download_item` is not part of any supported DZLL workflow.
- Steam library resolver is active:
  - find native Steam libraries from `libraryfolders.vdf`
  - find DayZ `appmanifest_221100.acf`
  - derive Workshop, downloads, compatdata, and Proton-prefix paths from the
    DayZ Steam library

## Server Companion Invariants

Server Companion is evidence-driven and conservative.

- Learning requires complete observational continuity:
  `coverage_complete=True` and `lifecycle_interruption is None`.
- Unconfirmed, incomplete, or lifecycle-interrupted events must contribute
  neither positive nor negative schedule-learning evidence.
- Diagnostic event classification is separate from learning eligibility;
  uncertain events may remain classified and persisted.
- Uncertain intervals may block unsupported inference; that is not negative
  evidence.
- Unmonitored time is not evidence that a restart did or did not occur.
- Preserve one physical restart -> one physical-event identity.
- Query-visible weak evidence is valid only when its specific evidence contract
  is satisfied.
- Do not casually alter scoring, harmonic, expected-window,
  incumbent/challenger, H1-H4, or warning policy.

## Be Careful With

- Join flow.
- Steam UGC download/cancel/preflight.
- Delete All.
- Single-delete queue.
- Steam library resolver.
- Server Companion restart/timetable logic.

Only touch these when the task explicitly asks.

## Preferred Workflow

- For audits:
  - inspect first
  - do not modify implementation/source files
  - `DZLL_MEMORY.md` may be updated only with genuinely durable findings
  - write the audit report under `devReports/`
- For edits:
  - make the smallest targeted change
  - do not refactor unrelated code
  - preserve established behaviour outside the requested scope
- After edits:
  - run `py_compile` or `compileall` where appropriate
  - run relevant focused tests
  - run the maintained full test suite when practical
  - run `git --no-pager diff --check`
  - inspect the final diff
  - list exact files changed in the report
  - summarize behaviour changed in the report
  - record exact test results in the report
  - confirm no mutating Git commands were used
- Reports must be written under `devReports/`.
- Do not print or repeat report contents in terminal/chat output.
- After writing the report, terminal/chat output must include the full absolute path to the report file so the user can locate it.

## User Preference

- Keep changes tightly scoped.
- Do not refactor unrelated code.
- Do not perform broad cleanup unless explicitly requested.
- Prefer investigation over guessing.
- Preserve existing architecture unless the task explicitly requires a change.
