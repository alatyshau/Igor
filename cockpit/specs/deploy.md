# Deploy — Spec

Specification of the install script that deploys the cockpit into a Context folder.

## Purpose

Install the cockpit (MCP + Stop hook + agent persona) into a Context folder so the user can open Claude Code there and have the full Igor setup loaded.

## Invocation

```
python /path/to/Igor.source.git/cockpit/deploy/install.py <context_folder_path>
```

The Igor.source.git repo's own absolute path is discovered relative to where `install.py` lives — no environment variables, no Duet dependency at install time.

## What `install.py` does

1. **Validate / scaffold `context.json`.** If absent, write a minimal one (`name`, `cockpit_config`). If present, leave user-owned keys untouched.
2. **Create `.claude/` tree.** `<context_folder>/.claude/sessions/` and `<context_folder>/.claude/output-styles/`.
3. **Render `settings.json` from `settings.template.json`.** Substitute placeholders with absolute paths discovered relative to `install.py`'s own location (sibling `mcp/`, `hooks/`):

   ```json
   {
     "outputStyle": "igor",
     "hooks": {
       "Stop": [
         {"hooks": [{"type": "command", "command": "python3", "args": ["/…/cockpit/hooks/stop.py"]}]}
       ]
     },
     "mcpServers": {
       "igor": {
         "command": "node",
         "args": ["/…/cockpit/mcp/dist/index.js"]
       }
     }
   }
   ```

   If a `settings.json` already exists, merge `hooks` and `mcpServers` entries without overwriting unrelated keys.
4. **Deploy persona.** Read `instructions/igor.md` from source; prepend a localization paragraph rendered from `context.json.cockpit_config.localization` (single string with language, user name, timezone); write to `<context_folder>/.claude/output-styles/igor.md`. Source `igor.md` itself ships without personal data — public repo.
5. **Create empty `objectives/` and `journal/`** if absent.
6. **Report** what was created, merged, or skipped.

## Scoping

### Per-context, per-Claude-Code-version

Settings files are scoped to one Context — different Contexts may have different versions of Igor installed if needed (e.g., during MCP migration). No global state on the user's machine; everything lives inside Context folders.

Duet's role (per `mcp__duet__orientation`) is at runtime: the agent uses it to resolve `git_folders` paths for promotion. Deploy itself does not depend on Duet.
