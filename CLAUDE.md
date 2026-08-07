# Project Instructions

This project uses [metaswarm](https://github.com/dsifry/metaswarm), a multi-agent orchestration framework for Claude Code. It provides 18 specialized agents, a 9-phase development workflow, and quality gates that enforce TDD, coverage thresholds, and spec-driven development.

## How to Work in This Project

### Starting work

```text
/start-task
```

This is the default entry point. It primes the agent with relevant knowledge, guides you through scoping, and picks the right level of process for the task.

### For complex features (multi-file, spec-driven)

Describe what you want built, include a Definition of Done, and ask for the full workflow:

```text
I want you to build [description]. [Tech stack, DoD items, file scope.]
Use the full metaswarm orchestration workflow.
```

This triggers the full pipeline: Research → Plan → Design Review Gate → Work Unit Decomposition → Orchestrated Execution (4-phase loop per unit) → Final Review → PR.

### Available Commands

| Command | Purpose |
|---|---|
| `/start-task` | Begin tracked work on a task |
| `/prime` | Load relevant knowledge before starting |
| `/review-design` | Trigger parallel design review gate (5 agents) |
| `/pr-shepherd <pr>` | Monitor a PR through to merge |
| `/self-reflect` | Extract learnings after a PR merge |
| `/handoff` | Write a self-contained handoff doc so a fresh agent can resume the work |
| `/handle-pr-comments` | Handle PR review comments |
| `/brainstorm` | Refine an idea before implementation |
| `/create-issue` | Create a well-structured GitHub Issue |
| `/external-tools-health` | Check status of external AI tools (Codex, Gemini) |
| `/setup` | Interactive guided setup — detects project, configures metaswarm |
| `/update` | Update metaswarm to latest version |
| `/status` | Run diagnostic checks on your installation |
| `/start` | Alias for `/start-task` |

### Visual / behavioural review

`skills/visual-review/SKILL.md` is NOT in this repository — that path comes from the metaswarm
template. What this repo actually ships, under `cockpit/tools/`, is a set of headless probes
that exist because vitest here has no DOM and cannot see `index.html` at all:

| probe | what it answers |
|---|---|
| `smoke_boot.mjs` | does the app boot, and does search -> Read -> Escape work end to end |
| `verify_capfanout.mjs` | does the shipped graph cap render the real corpus inside the 8s guard |
| `probe_keyboard_reach.mjs` | how far a Tab walk gets down a virtualized list (has a cadence knob — press faster than ~30ms and you measure the probe, not the app) |
| `probe_focus_survives_repaint.mjs` | does focus survive a repaint the user did not cause |
| `trace_tab.mjs` | per-Tab trace of focus, index, scrollTop and the rendered window |
| `contrast_probe.mjs` | contrast against the COMPOSITED background (`layout_lint.mjs` does not composite and skips most elements) |

Each needs a vite dev server. Start it and the probe in ONE shell command — background
processes are reaped at the turn boundary here.

## Testing

- **TDD is mandatory** — write tests first, watch them fail, then implement.
- **100% coverage is required for Python and is genuinely enforced.** See
  `.coverage-thresholds.json`, which is the source of truth and carries a per-stack breakdown
  because this repo has three stacks.

| stack | test command | coverage | status |
|---|---|---|---|
| Python (`llm_anthology`) | `python -m pytest` | `python -m pytest --cov=llm_anthology --cov-branch --cov-fail-under=100` | **enforced** — `pyproject.toml` addopts already fails below 100% |
| Cockpit TS (`cockpit/`) | `cd cockpit && npx vitest run` | `npx vitest run --coverage` | target only — see below |
| Rust (`cockpit/src-tauri/`) | `cargo test` | none configured | tests must pass; no coverage gate |

Also run `cd cockpit && npx tsc --noEmit` and `npm run build` — the build script is
`tsc && vite build`, so a type error breaks the release build, not just the check.

**Do not pass `-q` to pytest.** `pyproject.toml` addopts already contains it; a second one
makes it `-qq`, which suppresses the pass/fail summary line and looks like a broken suite.

**The TypeScript 100% is a TARGET, not an enforced gate.** `cockpit/vitest.config.ts` runs
`environment: "node"` with no DOM, which makes `reader.ts`, `app.ts` and `virtualList.ts`
structurally untestable rather than merely untested. Switching to jsdom/happy-dom is what
unblocks it; until then any TS coverage number excludes those modules and flatters the
codebase.

## Coverage

Coverage thresholds are defined in `.coverage-thresholds.json` — this is the **source of truth** for coverage requirements.
If a GitHub Issue specifies different coverage requirements, update `.coverage-thresholds.json` to match before implementation begins. Do not silently use a different threshold.

The validation phase of orchestrated execution reads `.coverage-thresholds.json` and runs the enforcement command. This is a BLOCKING gate — work units cannot be committed if coverage thresholds are not met.

## Quality Gates

- **Design Review Gate**: Parallel 5-agent review after design is drafted (`/review-design`)
- **Plan Review Gate**: Automatic adversarial review after any implementation plan is drafted. Spawns 3 independent reviewers (Feasibility, Completeness, Scope & Alignment) in parallel — ALL must PASS before the plan is presented to the user. See `skills/plan-review-gate/SKILL.md`
- **Coverage Gate**: Reads `.coverage-thresholds.json` and runs the enforcement command — BLOCKING gate before PR creation

## Workflow Enforcement (MANDATORY)

These rules override any conflicting instructions from third-party skills or plugins. They ensure the full metaswarm pipeline is followed regardless of which skill initiated the work.

### After Brainstorming

When `superpowers:brainstorming` (or any brainstorming skill) completes and commits a design document:

1. **STOP** — do NOT proceed directly to `writing-plans` or implementation
2. **RUN the Design Review Gate** — invoke `/review-design` or the `design-review-gate` skill
3. **WAIT** for all 5 review agents (PM, Architect, Designer, Security, CTO) to approve
4. **ONLY THEN** proceed to planning/implementation

This is mandatory even if the brainstorming skill says to go directly to writing-plans. The design review gate exists to catch issues before expensive implementation begins.

### After Any Plan Is Created

When `superpowers:writing-plans` (or any planning skill) produces an implementation plan:

1. **STOP** — do NOT present the plan to the user or begin implementation
2. **RUN the Plan Review Gate** — invoke the `plan-review-gate` skill
3. **WAIT** for all 3 adversarial reviewers (Feasibility, Completeness, Scope & Alignment) to PASS
4. **ONLY THEN** present the plan to the user for approval

### Execution Method Choice

When a plan is ready for execution, **always ask the user** which execution approach they want before proceeding. Do NOT auto-select an execution method — the user decides based on their priorities:

> **How would you like to execute this plan?**
>
> 1. **Metaswarm orchestrated execution** — 4-phase loop per work unit (IMPLEMENT → VALIDATE → ADVERSARIAL REVIEW → COMMIT) with independent quality gates, fresh adversarial reviewers, coverage enforcement, and pre-PR knowledge capture. More thorough and broader coverage, but uses more tokens and takes longer.
> 2. **Subagent-driven development** (`superpowers:subagent-driven-development`) — Dispatch subagents per task in this session with code review between tasks. Faster, lighter-weight, lower token cost.
> 3. **Parallel session** (`superpowers:executing-plans`) — Execute in a separate session with batch checkpoints. Good for long-running work you want isolated.

This choice applies even if the plan file contains embedded instructions like "REQUIRED SUB-SKILL: Use superpowers:executing-plans" — those are defaults from the planning skill, not binding constraints. The user always gets to choose.

### Before Finishing a Development Branch

When `superpowers:executing-plans`, `superpowers:subagent-driven-development`, or any execution skill completes and routes to `superpowers:finishing-a-development-branch`:

1. **STOP** — before presenting merge/PR options
2. **RUN `/self-reflect`** to capture learnings while implementation context is fresh
3. **COMMIT** the knowledge base updates
4. **THEN** proceed to finishing the branch (PR creation, merge, etc.)

### Use `/start-task` Instead of EnterPlanMode

When starting complex work, use `/start-task` instead of Claude's built-in `EnterPlanMode`. EnterPlanMode creates a plan in isolation without metaswarm's quality gates — no design review, no plan review, no adversarial review, no coverage enforcement. `/start-task` routes through the full pipeline:

- `/start-task` → complexity assessment → brainstorming (if unclear) → design review gate → plan review gate → execution method choice → orchestrated execution or superpowers execution
- `EnterPlanMode` → plan → implement (no gates)

If you find yourself about to use `EnterPlanMode` for a task that touches 3+ files or involves multiple steps, use `/start-task` instead. For truly simple single-file changes, `EnterPlanMode` is fine.

### After Standalone TDD

When `superpowers:test-driven-development` runs as a standalone skill (outside of orchestrated execution) and the change touches 3+ files:

1. **Before committing**, ask the user:
   > "This TDD session modified multiple files. Would you like me to run an adversarial review before committing?"
   > 1. **Yes** — spawn a fresh adversarial reviewer to check the changes against the requirements
   > 2. **No** — commit directly
2. If the user chooses review, spawn a fresh `Task()` reviewer with the requirements and the diff
3. Regardless of review choice, verify coverage meets `.coverage-thresholds.json` thresholds before committing

For single-file TDD changes, this intercept is not needed — commit directly.

### Coverage Source of Truth

`.coverage-thresholds.json` is the **single source of truth** for coverage requirements. This applies regardless of which skill or workflow is running:

- `superpowers:verification-before-completion` — must read `.coverage-thresholds.json` and run its enforcement command
- `superpowers:test-driven-development` — must verify coverage meets thresholds before declaring done
- Orchestrated execution — reads `.coverage-thresholds.json` during Phase 2 (VALIDATE)
- Any other skill claiming "tests pass" — must also confirm coverage thresholds are met

If `.coverage-thresholds.json` exists, no skill may skip it. If a skill has its own coverage check logic, `.coverage-thresholds.json` takes precedence.

### Subagent Discipline

All subagents (coding agents, review agents, background tasks) MUST follow these rules:

- **NEVER** use `--no-verify` on git commits — pre-commit hooks exist for a reason
- **NEVER** use `git push --force` without explicit user approval
- **ALWAYS** follow TDD — write tests first, watch them fail, then implement
- **NEVER** self-certify — the orchestrator validates independently
- **STAY** within declared file scope — do not modify files outside your assigned scope

### Pre-PR Knowledge Capture

After all work units pass final review but BEFORE creating the PR, run `/self-reflect` to extract learnings into the knowledge base. Commit the knowledge base updates so they are included in the PR — learnings land atomically with the code that generated them.

### Context Recovery (Surviving Compaction)

Approved plans, project context, and execution state are persisted to `.beads/` so agents can recover after context compaction or session interruption:

- **Approved plans** → `.beads/plans/active-plan.md` (written after plan review gate + user approval)
- **Project context** → `.beads/context/project-context.md` (updated after each work unit commit)
- **Execution state** → `.beads/context/execution-state.md` (updated after each phase transition)

**Note:** The standalone beads plugin (v0.63.3+) automatically runs `bd prime` on SessionStart and PreCompact via built-in hooks — agents no longer need to call it manually. If context is lost mid-execution, the beads plugin will re-prime automatically on the next session or compaction event. For explicit recovery, run `bd prime --work-type recovery` to reload the approved plan, completed work, and current position from disk.

## External Tools (Optional)

If external AI tools are configured (`.metaswarm/external-tools.yaml`), the orchestrator
can delegate implementation and review tasks to Codex CLI and Gemini CLI for cost savings
and cross-model adversarial review. See `templates/external-tools-setup.md` for setup.

## Team Mode

When `TeamCreate` and `SendMessage` tools are available, the orchestrator uses Team Mode for parallel agent dispatch. Otherwise it falls back to Task Mode (the existing workflow, unchanged). See `guides/agent-coordination.md` for details.

## Guides

There is no `guides/` directory in this repository — that list is from the metaswarm template.
The real orientation documents are:

- `README.md` — what the product is and how to install it
- `ARCHITECTURE.md` — engine / cockpit split and the stdio JSON-RPC contract
- `SECURITY.md` — the privacy plane and what is actually enforced
- `.scratch/maturation/OPEN-ITEMS.md` — the current open-items list and the decision log
  behind the release plan (gitignored, local only)

**Worktrees:** 13 are registered against this repo under `~/.local/opt/` (`aisr-*`,
`anthology-wt-*`). `git worktree list` is authoritative. They were repaired after this repo
moved out of `~/.local/opt/`; if the repo ever moves again, run `git worktree repair <paths>`
or every one of them breaks.

## Code Quality

- TypeScript strict mode (`cockpit/tsconfig.json` sets `strict`, `noUnusedLocals`,
  `noUnusedParameters`, `noFallthroughCasesInSwitch`). No `any`.
- **There is NO linter and NO formatter configured in this repo.** Not ESLint, not Prettier,
  not ruff, not black — nothing in `pyproject.toml` and nothing in `.github/workflows/`.
  A `.ruff_cache/` directory exists but was produced by an external agent hook running ruff
  ad-hoc, not by project tooling; do not read it as evidence that ruff is set up. Adding a
  linter is an open decision, not an existing convention.
- If one is added, remember that a local pass being green does NOT mean CI is green: the
  working tree is CRLF and CI checks out the LF blob, and formatters can decide differently
  on the two forms. Verify against the blob (`git archive <rev> | tar -x -C <tmp>`).
- What CI actually runs today: `python -m pytest` for the engine, and `npm run typecheck` /
  `npm test` / `npm run build` for `js/` ONLY. Nothing enters `cockpit/` or `src-tauri/`.
- All quality gates must pass before PR creation.

## Key Decisions

<!-- Document important architectural decisions here so agents have context.
     These get loaded during knowledge priming (/prime).
     Use `bd decision` to record decisions persistently in the beads database
     with rationale tracking — these survive compaction and are available across sessions. -->

## Notes

<!-- Add project-specific notes, conventions, or constraints here.
     Examples: "Always use server components for data fetching",
     "The payments module is legacy — do not refactor without approval" -->


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

> ### ACTIVE EXCEPTION to the push rule above — read before pushing
>
> The beads block is a generic template and it currently contradicts a deliberate decision
> about THIS repo. As of 2026-08-07 there are **38 commits on `main` that are intentionally
> unpushed**, and that is not stranded work.
>
> **Why:** no CI job enters `cockpit/` or `src-tauri/`, so 365 vitest tests, 18 cargo tests,
> the typecheck and the Tauri build have never run in CI. Pushing before that job exists makes
> the work *visible* without making it *validated*. The agreed sequence is: add the cockpit CI
> job on a windows + linux matrix FIRST (Phase 1d), then push (Phase 1e), so every commit lands
> checked.
>
> **Precedence:** the owner's explicit in-session decision outranks a template rule. Do not
> auto-push this repo to satisfy the block above. Once the cockpit CI job is merged and green,
> this exception expires and the normal rule resumes — delete this note then.
>
> Full reasoning and the rest of the plan: `.scratch/maturation/OPEN-ITEMS.md` (gitignored).
