# Agent Instructions

**Governance SSOT is [`CLAUDE.md`](CLAUDE.md)** — the testing and coverage gates, the workflow-enforcement rules, and the standing decision about whether to push are stated there once; this file must never restate or contradict them, and what is unique here is the shell guidance below plus the `bd`-generated block.
**Beads (issue tracker)** is described exactly once, in that generated block: run `bd prime` for the full workflow and `bd dolt push` to sync issue data.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:7510c1e2 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/SYNC_CONCEPTS.md for details and anti-patterns.

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->

## Before pushing: beads syncs issue data to a PUBLIC remote

Beads auto-pushes issue data to `refs/dolt/data` when a remote exists, and **this repo is
public**. Keep `git-push: false` in `.beads/config.yaml`, or keep the tracker free of anything
private.

The 2026-08-07 carve-out that held commits back from `main` while no CI job entered `cockpit/`
is **retired** — that job now exists (`.github/workflows/ci.yml`, windows + linux, blocking)
and the held commits were pushed. Push normally. `CLAUDE.md` keeps the retirement record,
including the commit count, so an old transcript quoting the carve-out cannot revive it and
this file does not restate a number that would drift.

## Windows caution on the "non-interactive flags" advice above

`-Force` is not a universal safety flag on this machine. **`New-Item -ItemType File -Force` on
an EXISTING file recreates it empty and destroys its content** — it does not mean "ensure
exists". Guard it (`if (-not (Test-Path $p)) { New-Item -ItemType File $p }`) and use
`Set-Content`/`Out-File` to write content. Prefer the agent Write tool over shell here-strings:
a here-string payload is scanned as if it were a command, and a document quoting a destructive
command gets the write blocked.
