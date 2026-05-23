# Journal Folder Schema (draft)

Structural contract for the `journal/` folder — the calendar of sessions for a context.

## Folder layout

```
journal/
  YYYY/                            ← year
    MM/                            ← month
      DD/                          ← day (DayFolder, Moscow timezone)
        HHMM_<slug>/               ← SessionFolder, see schemas/session_folder.md
```

**JournalFolder** — `journal/` at the root of the context.

**DayFolder** — `YYYY/MM/DD/`. Holds the SessionFolders for one day. Multi-day sessions are disallowed by design; each session belongs to exactly one day.

**SessionFolder** — see `schemas/session_folder.md` for the internal structure of one session.

## ContextFolder layout (overview)

For reference — the journal sits inside the broader context layout:

```
<ContextFolder>/
  context.json
  .claude/
    settings.json
    sessions/<session_id>.json     ← SessionStateFile; schema and writer in cockpit/specs/stop_hook.md §SessionStateFile
  objectives/                      ← see schemas/obj_folder.md
  journal/                         ← this schema
  shared/                          ← cross-OBJ resources
```
