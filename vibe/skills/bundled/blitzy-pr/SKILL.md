---
name: blitzy-pr
description: Validate a PR by running universal static checks and project-guide-specific checks, scoped to affected subprojects and tech stacks
allowed-tools: [Read, Glob, Grep, Bash, Agent]
---

# Blitzy PR — Mono-Repo PR Validator

You are validating a PR in the Blitzy mono-repo. You run universal static checks (lint, security, tests) plus project-guide-specific checks, all scoped to the files actually changed.

## Arguments

`$ARGUMENTS` may contain:

| Arg | Effect |
|-----|--------|
| `<PR number>` | Validate a specific PR (e.g., `blitzy-pr 42`) |
| (none) | Auto-detect from current branch |

## Phase 1 — Establish PR Context

**Detect the PR and changed files.** Reference: `references/pr-context.md`

1. If a PR number was given, run `gh pr view <number> --json number,title,baseRefName,headRefName,url` and `gh pr diff <number> --name-only`
2. Otherwise, detect from current branch: `gh pr view --json number,title,baseRefName,headRefName,url 2>/dev/null`
3. If no PR exists, fall back to branch diff: `git diff --name-only main...HEAD`
4. Store the **changed file list** — this drives everything downstream

**Print a summary:**
```
PR: #<number> — <title>
Branch: <head> → <base>
Changed files: <count>
```

## Phase 2 — Detect Subprojects and Tech Stack

From the changed file list:

1. **Extract affected subprojects** — the first path component of each changed file (e.g., `archie-service-admin/src/foo.py` → `archie-service-admin`)
2. **Detect tech stack per subproject:**
   - Any `.py` files changed → **Python** checks needed
   - Any `.ts`/`.tsx`/`.js`/`.jsx`/`package.json` changed → **Node** checks needed
   - Any `alembic/versions/*.py` changed → **Migration** checks needed
3. **Find Project Guides** — for each affected subproject, glob for:
   ```
   <subproject>/blitzy/documentation/Project Guide.md
   ```
   Also check: `<subproject>/blitzy/documentation/projectguide.md` (case variation)

**Print:**
```
Affected subprojects: <list>
Tech stacks: Python | Node | Both
Project guides found: <list or "none">
Migrations: yes | no
```

## Phase 3 — Universal Static Checks

Run these regardless of project guide content. **Scope all checks to changed files only.**

### 3a. Security Scan

Reference: `references/security-checks.md`

- Scan the diff (added lines only) for secrets, API keys, credentials
- Check if any sensitive files (`.env`, `*.key`, `credentials.json`) are in the changed list
- Severity: **BLOCKING**

### 3b. Python Checks (if Python stack detected)

Reference: `references/python-checks.md`

For each affected Python subproject:

1. Find the virtualenv (`.venv/bin/activate` or `venv/bin/activate` in subproject root)
2. Run `black --check` on changed `.py` files — **BLOCKING**
3. Run `isort --check-only` on changed `.py` files — **BLOCKING**
4. Run `python3 -m py_compile` on each changed `.py` file — **BLOCKING**
5. Run `pytest` in the subproject (if test dir exists) — **BLOCKING**

If a tool is not installed, record as **SKIP**.

### 3c. Node Checks (if Node stack detected)

Reference: `references/node-checks.md`

For each affected Node subproject:

1. Check for `tsconfig.json` — if present, run `yarn tsc --noEmit` — **BLOCKING**
2. Run `yarn test` or `npm test` (check `package.json` for test script) — **BLOCKING**
3. Run lint if available — **WARNING**

### 3d. Migration Checks (if migrations detected)

Reference: `references/migration-checks.md`

1. Find `alembic.ini` in the subproject
2. Run `alembic upgrade head` + `alembic downgrade -1` round-trip — **BLOCKING**
3. Check for multiple heads — **WARNING**
4. If no DB connection available, record as **SKIP**

### 3e. Diff Scope Check

Compare the changed file list against the PR title/branch name:
- Flag files that seem unrelated to the feature (e.g., changes in 5+ unrelated subprojects)
- Severity: **WARNING**

## Phase 4 — Project Guide Checks

For each Project Guide found in Phase 2:

1. **Read the guide** and find the **Development Guide** section. Search by content, not section number — the heading varies:
   - `## Development Guide`
   - `## Section 9 — Development Guide`
   - `## 9. Development Guide`
   - `## 5. Development Guide`
   - `## Comprehensive Development Guide`
   - Or any `##` heading containing "Development Guide"

2. **Extract the checklist steps** from that section — these are typically numbered setup/build/test commands.

3. **Execute each step** that is relevant to validation (skip environment setup steps like "install Python 3.12"):
   - Run the exact command from the guide
   - Record pass/fail based on exit code
   - Capture error output on failure
   - Severity: **INFO** (these layer on top of universal checks, not replace them)

4. If no Project Guide exists for an affected subproject, note it and move on — universal checks still cover it.

## Phase 5 — Report Results

Present a single consolidated table with severity levels:

```markdown
## PR Validation Results

**PR:** #<number> — <title>
**Subprojects:** <list>
**Stacks:** <Python | Node | Both>

| # | Check | Scope | Status | Severity | Notes |
|---|-------|-------|--------|----------|-------|
| 1 | Secrets scan | PR diff | PASS | BLOCKING | |
| 2 | Sensitive files | PR diff | PASS | BLOCKING | |
| 3 | black --check | archie-service-admin | PASS | BLOCKING | |
| 4 | isort --check | archie-service-admin | PASS | BLOCKING | |
| 5 | py_compile | archie-service-admin | FAIL | BLOCKING | src/foo.py: SyntaxError line 42 |
| 6 | pytest | archie-service-admin | PASS | BLOCKING | 14 passed |
| 7 | Alembic round-trip | db-common-model | SKIP | BLOCKING | No DB connection |
| 8 | Diff scope | PR | PASS | WARNING | |
| 9 | Guide: build project | archie-service-admin | PASS | INFO | |
| 10 | Guide: run migrations | archie-service-admin | SKIP | INFO | No migration files changed |

### Summary

- **BLOCKING:** X passed, Y failed, Z skipped
- **WARNING:** X passed, Y flagged
- **INFO:** X passed, Y skipped

**Result:** [ALL CLEAR | N BLOCKING FAILURES — must fix before merge]
```

If any BLOCKING check failed, include the error output below the table under a `### Failures` section.

## Rules

- Always scope checks to changed files — never lint or test the entire repo
- Use exit codes for pass/fail, never hardcoded counts
- If a tool is missing, SKIP — don't fail the whole run
- Run checks per-subproject when the subproject has its own venv/node_modules
- The Project Guide checks are additive — they don't replace universal checks
- Be explicit about what was skipped and why
- If `$ARGUMENTS` is empty and you can't detect a PR or branch diff, ask the user which PR to validate
