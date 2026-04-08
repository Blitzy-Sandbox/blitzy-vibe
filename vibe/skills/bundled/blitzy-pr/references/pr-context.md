# PR Context Detection

## Detect Current Branch and PR

```bash
# Current branch
git branch --show-current

# Associated PR (if any)
gh pr view --json number,title,baseRefName,headRefName,url 2>/dev/null

# If no PR exists, work from branch diff against main/master
git log --oneline main..HEAD 2>/dev/null || git log --oneline master..HEAD 2>/dev/null
```

## Get Changed Files

```bash
# From PR
gh pr diff --name-only 2>/dev/null

# Fallback: from branch diff
git diff --name-only main...HEAD 2>/dev/null || git diff --name-only master...HEAD 2>/dev/null

# Fallback: uncommitted changes
git diff --name-only HEAD
```

## Detect Affected Subprojects

From the changed file list, extract unique top-level directories. Each is a subproject.
Map each subproject to its `blitzy/documentation/Project Guide.md` if one exists.

## Detect Tech Stack From Changed Files

- `*.py` → Python stack
- `*.ts`, `*.tsx`, `*.js`, `*.jsx`, `package.json` → Node/TypeScript stack
- `alembic/**`, `**/versions/*.py` under alembic → Migration checks needed
- `*.md` in `blitzy/documentation/` → Documentation-only change (lighter checks)
