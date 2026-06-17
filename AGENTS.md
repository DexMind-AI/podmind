# podmind — Agent Instructions (pipeline repo)

This repo is the **pipeline** — the Python package, CLI, scripts, and cron orchestration that turn Pocket Casts + YouTube history into a Karpathy-style LLM wiki. The **data and wiki live in a separate vault** directory (`$PODMIND_DATA_ROOT`), installed editable so edits here take effect immediately with no rebuild step.

> **This is the shared, agent-agnostic source of truth for working on the code.** Any coding agent — Claude Code, Codex, Gemini CLI, Hermes — should follow it. `CLAUDE.md` and `GEMINI.md` are thin shims that import this file (`@AGENTS.md`); put harness-specific notes there, not here.
>
> Two adjacent docs, do not duplicate them:
> - **`docs/AGENTS-vault.md`** — the *vault* schema/agent-instructions (what a well-formed wiki looks like). That governs an agent *maintaining a vault*, not editing this code.
> - **`docs/OPERATIONS.md`** — operator-facing detail: LLM provider config, cost, the `podmind` verb→script map, launchd automation, transcript cascade.

## ⚠ Editing a live engine — discipline before you save

Because the package is installed editable into the vault, a bad save breaks production **immediately**. Two launchd jobs depend on this code:

- **`cron/whisper.sh`** (`com.podcast-wiki.whisper`, RunAtLoad + KeepAlive) re-spawns Python every batch (~30 min). A syntax error in `podmind/transcript.py` silently fails every batch.
- **`cron/daily.sh`** (04:00) breaks if any imported module fails; it writes its own log entry to the vault's `wiki/log.md` so the failure is visible.

Before editing:

1. **`uv run pytest tests/`** — 100+ tests, ~0.15s. Non-negotiable, run it before and after.
2. For `transcript.py`, `youtube.py`, or anything the launchd jobs import: **smoke-test imports** with `uv run python -m podmind.<module> --help` to surface import errors immediately.
3. **`./bin/preflight.sh`** before any edit to `cron/daily.sh`, `cron/whisper.sh`, or anything they invoke — 47 checks (~10s): launchd env, PATH-resolvability of every binary, podmind imports, bin/ scripts, vault tree shape, plist symlinks, `daily_digest --dry-run`.
4. After committing, watch the whisper log for one cycle (~5 min) to confirm it still runs cleanly.

**Don't refactor `podmind/paths.py` or `bin/_lib.py` without extra care** — they're the import-time entry points; a bug there poisons everything.

## Layout

```
podmind/        Python package — pipeline modules (paths, slugs, llm, transcript,
                youtube, pocketcasts, sync, refresh_badges, curation, embeddings, …)
                cli.py is the `podmind` front-door router (subprocess-dispatches to bin/).
bin/            Executable scripts; tick_prep.py / summarize.py / tick_finalize.py are
                the ingest pipeline. Some scripts are maintainer-only (stripped from the
                public mirror by scripts/publish_public.sh).
cron/           launchd plists (+ .template) and the daily.sh / whisper.sh wrappers.
                Read $PODMIND_DATA_ROOT and cd there on entry.
scripts/        demo.sh, install_launchd.sh, publish_public.sh.
docs/           AGENTS-vault.md (vault schema), OPERATIONS.md.
tests/          pytest suite; package hierarchy mirrors podmind/.
```

## Conventions

- **Package manager / runner: `uv`.** `uv run pytest`, `uv run python -m podmind.<module>`. Don't invoke system Python.
- **Python style:** strong type hints in implementation and tests; idiomatic, concise, DRY; avoid defensive error handling unless it improves diagnostics; no redundant comments.
- **Tests:** comprehensive pytest coverage for changed behavior; generalize shared setup into fixtures; keep the test hierarchy aligned with the package. Mark integration tests with a shared package-level marker.
- **LLM provider is configurable** (`PODMIND_LLM_*` env / `llm_*` secrets) and provider-neutral by design — DeepSeek V4 is the *code* default (`podmind/llm.py`); the wire protocols are OpenAI-compatible and Anthropic Messages. Don't hardcode a provider. See `docs/OPERATIONS.md`.
- **Secrets** live in `~/.config/podmind/secrets.json` (and env); never commit keys or `$PODMIND_DATA_ROOT` contents.
- **Two-repo discipline:** all executable code lives here; the vault holds only data. Don't add code to the vault or data to this repo.
- **Public mirror:** publish with `scripts/publish_public.sh <public-repo-dir>` from `main` — it strips maintainer-private scripts and internal docs and rsyncs into the mirror preserving its `.git`. Don't hand-roll `git archive`.

## Schema co-evolution

If you change behavior future agents/sessions must know about (a new script, a moved entry point, a changed contract), update this file in the same change. Vault-schema changes go in `docs/AGENTS-vault.md` instead.
