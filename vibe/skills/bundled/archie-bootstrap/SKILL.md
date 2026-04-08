---
name: archie-bootstrap
description: Set up the Blitzy dev environment with archie-bootstrap and persist shell state to ~/.archie/
allowed-tools: [Read, Write, Glob, Grep, Bash]
---

# Archie Bootstrap — Dev Environment Setup with Shell Persistence

You are setting up the Blitzy development environment by running `archie-bootstrap` and persisting the shell environment so it survives after the agent closes.

Environment snapshots are stored globally at `~/.archie/<repo-tag>-env` (where `<repo-tag>` is the project directory name). A shell hook wrapping `blitzy` auto-sources the env file after `blitzy bootstrap` exits.

## Arguments

`$ARGUMENTS` may contain:

| Arg | Effect |
|-----|--------|
| `dev` | Bootstrap for development (default) |
| `ci` | Bootstrap for CI environment |
| `--skip-make` | Skip the `make` build step |
| (empty) | Same as `dev` |

## Step 1: Detect Project Root

Find the project root by looking for these markers (walk up from cwd):
- `archie-bootstrap` script
- `Makefile` with archie targets
- `~/.archie/<repo-tag>-env` (already bootstrapped)

If a snapshot already exists in `~/.archie/`, ask the user:
> "Environment snapshot `~/.archie/<tag>-env` already exists. Re-bootstrap? (This will overwrite the existing snapshot.)"

## Step 2: Capture Environment Before

Save the current environment state:

```bash
env | sort > /tmp/archie-before.env
```

## Step 3: Run archie-bootstrap

Run the bootstrap in a subshell that captures the resulting environment:

```bash
bash -c 'source ./archie-bootstrap $ARGUMENTS 2>&1 && env | sort' > /tmp/archie-after.env
```

If the bootstrap fails, report the error output and stop.

## Step 4: Generate Environment Snapshot

Compute the environment delta and write it to `~/.archie/<repo-tag>-env`:

```bash
mkdir -p ~/.archie
REPO_TAG="$(basename "$PWD")"
ENV_FILE="$HOME/.archie/${REPO_TAG}-env"

# Find new/changed env vars
comm -13 /tmp/archie-before.env /tmp/archie-after.env | sed 's/^/export /' > "$ENV_FILE"

# Add venv activation if VIRTUAL_ENV was set
grep -q VIRTUAL_ENV "$ENV_FILE" && echo 'source "$VIRTUAL_ENV/bin/activate" 2>/dev/null' >> "$ENV_FILE"
```

## Step 5: Verify

Source the snapshot in a fresh subshell and verify key env vars are present:

```bash
REPO_TAG="$(basename "$PWD")"
bash -c "source ~/.archie/${REPO_TAG}-env && echo \"VIRTUAL_ENV=\$VIRTUAL_ENV\" && echo \"PATH includes venv: \$(echo \$PATH | grep -c venv)\""
```

## Step 6: Report and Print Shell Instructions

Print the results and the exact commands for the user:

```
Environment snapshot written to ~/.archie/<repo-tag>-env

If the blitzy shell hook is installed, your venv will activate automatically
after running `blitzy bootstrap`.

To activate manually:
  source ~/.archie/<repo-tag>-env
```

**Important:** The `blitzy bootstrap` CLI command handles shell hook installation interactively. This skill should NOT modify the user's shell config — only print instructions.

## Rules

- Never modify ~/.zshrc, ~/.bashrc, or any shell config file — only print instructions
- Always write the env snapshot to `~/.archie/<repo-tag>-env` (never to the project root)
- The env file must be sourceable (`source ~/.archie/<tag>-env` must work)
- Clean up /tmp/archie-before.env and /tmp/archie-after.env after generating the snapshot
- If archie-bootstrap doesn't exist in the project, stop: "No archie-bootstrap script found. This skill requires a Blitzy project with archie-bootstrap."

## Composability

After bootstrap, suggest:
- "Run `/blitzy-onboarding` to continue the full developer onboarding flow."
- "Run `/onboard` to generate an architecture overview of this codebase."
