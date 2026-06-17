# Gemini CLI — podmind (pipeline repo)

@AGENTS.md

The file above is the shared, agent-agnostic source of truth for working on this codebase. Read and follow it. This file adds **only Gemini-specific** notes; keep it thin (amend `AGENTS.md`, not here).

## Notes

- This repo is the engine; the wiki vault is a separate directory with its own `AGENTS.md`. Content questions are vault operations, not code changes.
- The vault schema is `docs/AGENTS-vault.md`; operator detail is `docs/OPERATIONS.md`.
- Run the pre-edit discipline in `AGENTS.md` literally (`uv run pytest tests/`, import smoke-tests, `./bin/preflight.sh` before cron edits) — it guards a live launchd pipeline.
