---
name: commit
description: Stage and commit changes with conventional commit messages, with branch safety and auto-generated messages
allowed-tools: [Glob, Grep, Read, "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git branch:*)", "Bash(git checkout:*)", "Bash(git switch:*)", "Bash(git add:*)", "Bash(git commit:*)", "Bash(git commit --amend:*)", "Bash(git rev-parse:*)", "Bash(git stash:*)"]
---

# Commit — Conventional Commits with Branch Safety

You are staging and committing changes in a git repository using conventional commit conventions.

## Arguments

`$ARGUMENTS` may contain any combination of:

| Flag | Effect |
|------|--------|
| `--amend` | Amend the previous commit instead of creating a new one |
| `--fixes #N` | Link commit to issue N with `Fixes #N` footer |
| `--closes #N` | Link commit to issue N with `Closes #N` footer |
| `--quick` | Skip confirmation prompt — auto-detect and commit immediately |
| `--wip` | Create a `wip: <description>` commit with no body required |
| *(plain text)* | Used as the commit message description |

## Step 1: Pre-flight Checks

1. Run `git rev-parse --is-inside-work-tree` to confirm this is a git repo. If not, stop and tell the user.
2. Run `git status --porcelain` to check for changes. If there are no staged, unstaged, or untracked changes, stop and tell the user there's nothing to commit.
3. Check for detached HEAD with `git rev-parse --abbrev-ref HEAD`. If it returns `HEAD`, warn the user they're in detached HEAD state and ask how to proceed.

## Step 2: Branch Safety

1. Get the current branch: `git rev-parse --abbrev-ref HEAD`
2. If the branch is any of: `main`, `master`, `develop`, `development`, `qa`, `prod`, `production`, `staging`, `release` — **warn the user** that they're on a protected branch.
3. Ask the user for a branch name, suggesting one based on the changes (see Step 3 for naming convention).
4. Create the branch with `git checkout -b <branch-name>`.
5. If the user explicitly says they want to commit directly to the protected branch, proceed — but confirm once more before committing.

## Step 3: Branch Naming Convention

When suggesting or creating branches, use this format:

```
<type>/<kebab-case-description>
```

- **Type** matches the conventional commit type: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `style`, `perf`, `ci`, `build`
- **Description**: 2-5 words, kebab-case, under 60 chars total
- Examples: `feat/add-user-auth`, `fix/null-check-parser`, `docs/update-api-reference`, `chore/bump-dependencies`

## Step 4: Analyze Changes

Run these commands to understand the full picture:

1. `git diff` — unstaged changes
2. `git diff --cached` — staged changes
3. `git status --porcelain` — all changes including untracked files

If diffs are large, read key changed files to understand the nature of the changes.

### Breaking Change Detection

Scan the diff for signals that this is a breaking change:

- **Removed or renamed** public functions, classes, or exported symbols
- **Changed function signatures** (added required params, changed return types)
- **Deleted files** that other modules import
- **Renamed environment variables** or config keys

If breaking changes are detected:
1. Add `!` after the type/scope in the commit subject: `feat(api)!: rename auth endpoint`
2. Add a `BREAKING CHANGE:` footer describing what changed and migration steps

## Step 5: Safety Scan

Check for sensitive files in the changeset. **Warn and exclude** any of these:

- `.env`, `.env.*` (environment variables)
- `*.pem`, `*.key`, `*.p12`, `*.pfx` (certificates/keys)
- `credentials.json`, `service-account.json` (API credentials)
- `id_rsa`, `id_ed25519`, `*.pub` (SSH keys)
- `*.sqlite`, `*.db` (databases)
- `.npmrc`, `.pypirc` (package registry auth)
- `*.secret`, `*_secret*`

If any are found:
1. Tell the user which files were excluded and why
2. Suggest adding them to `.gitignore` if not already present
3. Proceed with the remaining files

## Step 6: Stage Files

- Use `git add <file1> <file2> ...` with explicit file paths. **Never** use `git add -A` or `git add .`.
- If there are already staged changes and no unstaged changes, skip staging and use what's already staged.
- If there are both staged and unstaged changes, ask the user whether to include the unstaged changes or commit only what's staged.
- If there are more than 15 files to stage, list them and confirm with the user before staging.

## Step 7: Determine Commit Message

### If `--wip` flag is present:
- Use `wip: <brief description of current state>` as the commit message
- No body required, skip type detection
- Skip commit message validation rules (length/mood checks)

### If `$ARGUMENTS` contains a message:
- If it's already in conventional commit format (e.g., `fix: resolve null pointer`), use it as-is.
- If it's a plain description (e.g., `fix the login bug`), convert it to conventional format.

### If `$ARGUMENTS` is empty:
Auto-detect the commit type from the diff:

| Type | Heuristic |
|------|-----------|
| `feat` | New files, new functions/methods/components, new exports |
| `fix` | Changes to existing logic, error handling, edge cases |
| `refactor` | Restructuring without behavior change, renames, moves |
| `docs` | Only `.md` files, comments, or docstrings changed |
| `chore` | Dependency updates, config files, maintenance |
| `test` | Test files added or modified |
| `style` | Formatting, whitespace, linting fixes |
| `perf` | Optimization, caching, reducing allocations |
| `ci` | CI/CD config files (`.github/workflows`, Jenkinsfile, etc.) |
| `build` | Build scripts, Dockerfile, Makefile changes |

### Issue linking
If `--fixes #N` or `--closes #N` was passed in `$ARGUMENTS`, append the corresponding footer:
```
Fixes #123
```
or
```
Closes #456
```

### Message format:
```
<type>(<optional-scope>): <imperative-description>
```

- Use **imperative mood** ("add", "fix", "update" — not "added", "fixes", "updated")
- **Lowercase** first word after the colon
- **No period** at the end
- Keep the subject line **under 72 characters**
- Add a **body** (separated by blank line) for non-trivial changes explaining *why*, not *what*

### Commit Message Validation
Before committing, verify the message passes these checks:
1. Subject line is under 72 characters
2. No trailing period on the subject
3. First word after the colon is lowercase
4. Uses imperative mood (not past tense like "added", "fixed", "updated")
5. Has a valid conventional commit type prefix

If any check fails, fix the message before proceeding.

## Step 7.5: Amend Mode (`--amend`)

If `--amend` was passed in `$ARGUMENTS`:

1. Run `git log --oneline -1` to show the commit being amended
2. Run `git log -1 --format=%P` to check parent count — **refuse to amend merge commits**
3. Run `git log -1 --format=%H` and `git branch -r --contains HEAD` to check if the commit has been pushed — **warn the user** if it has, as amending will require a force push
4. Show the user the current commit message and the proposed changes
5. Use `git commit --amend` instead of `git commit` in Step 8
6. If the user wants to change the message, use `git commit --amend -m "new message"`. Otherwise use `git commit --amend --no-edit` to keep the existing message.

## Step 8: Confirm and Commit

### If `--quick` flag is present:
- Skip the confirmation prompt
- Auto-detect the type, generate the message, stage, and commit immediately
- Still enforce safety scans (Step 5) and branch protection (Step 2)
- Still validate the commit message (Step 7)
- Show the post-commit summary (Step 9)

### Standard flow:
Present a summary to the user:

```
Branch:  <branch-name>
Staged:  <number> file(s)
Message: <proposed commit message>

Files:
  - path/to/file1.ts
  - path/to/file2.ts
```

- Wait for the user to approve, edit the message, or cancel.
- **Never** append a `Co-Authored-By` or `Co-authored-by` trailer to the commit message.
- Execute the commit using a heredoc for proper formatting:
  ```
  git commit -m "$(cat <<'EOF'
  <type>(<scope>): <description>

  <optional body>

  <optional footers>
  EOF
  )"
  ```
- Verify with `git log --oneline -1`.

## Step 9: Post-commit Summary

After a successful commit, show:

```
Committed: <short-hash> <commit message>
Branch:    <branch-name>
Files:     <count> changed
```

Then suggest: "Run `git push` to push your changes." — but **do not execute `git push`**.

## Step 10: Multi-commit Workflow

When the user requests splitting changes into multiple commits (e.g., "commit these separately", "split into logical commits"):

1. **Group files by change type** — separate features from fixes from refactors from docs
2. **Present the proposed grouping** to the user:
   ```
   Commit 1 (feat): path/to/new-feature.ts, path/to/component.tsx
   Commit 2 (fix):  path/to/bugfix.ts
   Commit 3 (docs): README.md
   ```
3. Let the user adjust groupings before proceeding
4. **Execute sequentially** — stage and commit each group one at a time
5. **Show combined summary** at the end:
   ```
   Created 3 commits:
     abc1234 feat(ui): add user profile component
     def5678 fix(api): handle null response in auth
     ghi9012 docs: update API reference
   ```

## Edge Cases

- **Only staged changes**: Skip staging, commit what's already staged.
- **Mixed staged/unstaged**: Ask the user whether to include unstaged changes.
- **Merge conflicts**: If `git status` shows merge conflicts, tell the user to resolve them first.
- **Untracked files only**: Ask the user which files to add.
- **Detached HEAD**: Warn and ask for instructions before committing.
- **Empty diff after staging**: If staged changes result in no diff (e.g., only whitespace), warn the user.
