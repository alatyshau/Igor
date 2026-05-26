# Deploy — Spec

Specification of the install script that deploys the cockpit into a Context folder.

## Purpose

Install the cockpit (MCP + hooks + agent persona) into a Context folder so the user can open Claude Code there and have the full Igor setup loaded.

## Invocation

```
python /path/to/Igor.source.git/cockpit/deploy/install.py <context_folder_path>
```

The Igor.source.git repo's own absolute path is discovered relative to where `install.py` lives — no environment variables, no Duet dependency at install time.

## What `install.py` does

1. **Validate / scaffold `context.json`.** If absent, write a minimal one (`name`, `cockpit_config`). If present, leave user-owned keys untouched.
2. **Create `.claude/` tree.** `<context_folder>/.claude/sessions/`, `<context_folder>/.claude/output-styles/`, and `<context_folder>/.claude/agents/` (canonical Claude Code path for subagent profiles — auto-discovered on session start).
3. **Render `settings.json` from `settings.template.json`.** Substitute placeholders with absolute paths discovered relative to `install.py`'s own location (sibling `mcp/`, `hooks/`). The cockpit registers both hooks — `UserPromptSubmit` (pre-turn bootstrap) and `Stop` (post-turn persistence + late-fill) — see [`stop_hook.md`](stop_hook.md) §Bootstrap responsibility:

   ```json
   {
     "outputStyle": "igor",
     "hooks": {
       "UserPromptSubmit": [
         {"hooks": [{"type": "command", "command": "python3", "args": ["/…/cockpit/hooks/user_prompt_submit.py"]}]}
       ],
       "Stop": [
         {"hooks": [{"type": "command", "command": "python3", "args": ["/…/cockpit/hooks/stop.py"]}]}
       ]
     }
   }
   ```

   If a `settings.json` already exists, merge `hooks` entries without overwriting unrelated keys.

4. **Render `.mcp.json` for MCP server registration.** Claude Code's project-level MCP config lives at the Context root, not inside `settings.json`. Register the cockpit's MCP server here:

   ```json
   {
     "mcpServers": {
       "igor-cockpit": {
         "type": "stdio",
         "command": "node",
         "args": ["/…/cockpit/mcp/dist/src/index.js"]
       }
     }
   }
   ```

   Merge with any existing `.mcp.json` entries without overwriting unrelated MCP servers.
5. **Deploy persona.** Read `instructions/igor.md` from source; substitute the `__LOCALIZATION_TEXT__` placeholder (inside the `## Identity` section) with `context.json.cockpit_config.localization` (single string with language, user name, timezone). If the field is absent in `context.json`, substitute a neutral default (`"English-speaking. Single-user context. Timezone: system default."`). Write the result to `<context_folder>/.claude/output-styles/igor.md`. Source `igor.md` itself ships without personal data — public repo.
6. **Deploy subagent profiles.** Copy every `instructions/subagents/*.md` from source to `<context_folder>/.claude/agents/<name>.md` (canonical Claude Code path — auto-discovered on session start, available via `@agent-<name>` mention and `/agents`). Overwrite existing files; prune deployed `*.md` whose name is absent from source — profiles are source-of-truth in the repo, not user-editable in the Context. **Dual visibility:** the same path serves both Claude Code's native discovery *and* MCP `spawn_subchat` (which reads `<ContextFolder>/.claude/agents/<name>.md` to materialize per-session subchats). Cockpit subagents are still invoked through `subchat` only — `instructions/igor.md` forbids invocation via the Task tool — native registration is for visibility, not for changing the runtime contract.
7. **Create empty `objectives/` and `journal/`** if absent.
8. **Report** what was created, merged, or skipped.

## Scoping

### Per-context, per-Claude-Code-version

Settings files are scoped to one Context — different Contexts may have different versions of Igor installed if needed (e.g., during MCP migration). No global state on the user's machine; everything lives inside Context folders.

Duet's role (per `mcp__duet__orientation`) is at runtime: the agent uses it to resolve `git_folders` paths for promotion. Deploy itself does not depend on Duet.
