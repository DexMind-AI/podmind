# Claude Code — podmind (pipeline repo)

@AGENTS.md

The file above is the shared, agent-agnostic source of truth for working on this codebase. Read and follow it. This file adds **only Claude-Code-specific** notes; keep it thin (amend `AGENTS.md`, not here).

## Notes

- **This repo is the engine; the wiki vault is separate.** When the user's question is about *their podcast/listening content* rather than this code, that's a vault operation — work happens in the vault repo under its own `AGENTS.md`/`CLAUDE.md`, not here.
- The vault schema (what a well-formed wiki looks like) is `docs/AGENTS-vault.md` — reference it, don't reimplement it here.
- Run the pre-edit discipline in `AGENTS.md` literally: `uv run pytest tests/`, import smoke-tests, `./bin/preflight.sh` before touching cron. These guard a live launchd pipeline.
- For multi-file pipeline changes, you can fan out reading/analysis to subagents, but make the actual edits yourself and re-run the test suite before claiming done.
