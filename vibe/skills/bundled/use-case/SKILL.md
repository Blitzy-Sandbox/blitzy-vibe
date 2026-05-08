---
name: use-case
description: Generate a formatted Blitzy Use Cases Report section from a GitHub PR
allowed-tools: [Read, Glob, Grep, "Bash(gh:*)"]
---

# Blitzy Use Case Report Section Generator

## Purpose

Generate a formatted use case report section from a GitHub PR, matching the exact template used in the Blitzy 4.0 Use Cases Report. Output is ready to copy-paste into the report document.

## Arguments

`$ARGUMENTS` — GitHub PR URL (required). Example: `https://github.com/Blitzy-Sandbox/blitzy-designer/pull/1`

## Data Collection

Fetch PR metadata and diff stats using `gh` CLI:

```
! gh pr view $ARGUMENTS --json title,body,commits,additions,deletions,changedFiles,labels,files,headRefName,baseRefName
```

```
! gh pr diff $ARGUMENTS --stat
```

## Analysis Steps

1. **Extract PR metadata**: title, description, commit count, additions, deletions, changed files, file list
2. **Classify use case category** from the PR content into one of:
   - **Greenfield App** — New full-stack application built from scratch
   - **Greenfield Service** — New backend service or API built from scratch
   - **Language Migration** — Porting an existing codebase to a different language
   - **Framework Migration** — Migrating between frameworks within the same language
   - **Audit & Refactor** — Code quality, security, or compliance audit with refactoring
   - **Feature Addition** — Major new feature added to an existing application
   - **Integration** — Connecting systems, APIs, or services together
   - **Infrastructure** — DevOps, CI/CD, deployment, or infrastructure-as-code
   - **Design System** — UI component library or design system implementation
3. **Determine what was built**: Parse PR body, file diff stats, and commit messages to identify major components, features, and deliverables
4. **Calculate key results**:
   - **Completion %**: Estimate based on PR body (look for task lists, remaining work mentions). Default to 95% if unclear.
   - **Codebase**: total lines (additions + deletions as proxy), files changed, commit count
   - **Tests**: Look for test files in the diff stats, count test files and extract pass counts if available
5. **Identify remaining work**: Parse PR body for TODOs, follow-ups, or incomplete items
6. **Determine buyer**: Infer target audience from the use case category and what was built
7. **Write one-liner**: Create a quotable summary sentence capturing the key achievement

## Output Format

Generate the following markdown section. Replace all bracketed placeholders with actual data from the PR.

Use `N.` as the section number placeholder — the user will replace it with the correct number.

---

### Generated Section

```markdown
## N. [Category] - [Title derived from PR]

[1-2 sentence narrative describing the buyer scenario and what Blitzy built. Frame it as: a [buyer type] needed [outcome], so Blitzy [what it did]. Include a concrete detail like tech stack or scale.]

**One-liner:** "[Single quotable sentence summarizing the achievement — focus on speed, scale, or capability delivered]"

**Buyer:** [Target audience — e.g., "Early-stage startups needing to ship MVPs fast" or "Enterprise teams modernizing legacy systems"]

### What was built
- **[Component/Feature label]** — [Brief description of what this component does]
- **[Component/Feature label]** — [Brief description]
- [Continue for each major deliverable...]

### Key results
- **Completion** — [X]%
- **Codebase** — [additions + deletions] lines across [changedFiles] files, [commit count] commits
- **Tests** — [N test files or test count] passing, [pass rate if available]
- **Tech stack** — [Key technologies detected from file extensions and PR content]

### Remaining work
- [N] tasks ([brief comma-separated descriptions, or "None identified" if PR appears complete])

### Artifacts
- [Repository/project name extracted from PR URL]
- [Full GitHub PR URL]
```

---

### Summary Table Row

Also generate a summary table row for the report's Summary section:

```markdown
| [Category] - [Short Title] | [Buyer] | [One-liner without quotes] |
```

---

## Output Instructions

1. Output the generated section and summary table row directly in the conversation
2. Do NOT create or modify any files
3. If the PR URL is invalid or `gh` commands fail, report the error clearly
4. If PR body is sparse, do your best with available data from the diff stats and file names
5. Keep the narrative opener concise (1-2 sentences max)
6. Keep the one-liner punchy and under 20 words
7. For the "What was built" section, group related files into logical components rather than listing individual files
