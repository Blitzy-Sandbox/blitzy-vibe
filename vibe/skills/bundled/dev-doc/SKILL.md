---
name: dev-doc
description: Clean and standardize Mintlify documentation pages to match Blitzy docs conventions
allowed-tools: [Read, Write, Edit, Glob, Grep]
---

# Dev Doc — Mintlify Documentation Standardizer

You are cleaning and standardizing Mintlify `.mdx` documentation pages to match the Blitzy docs style guide.

## Arguments

`$ARGUMENTS` may contain any combination of:

| Argument | Effect |
|----------|--------|
| `<file-path>` | Target `.mdx` file to clean (required) |
| `--check` | Audit only — report issues without editing |

If no file path is provided, ask the user which file to clean. If a directory is given, list `.mdx` files in that directory using Glob and ask the user to pick one.

## Step 1: Read Target File

Read the file specified in `$ARGUMENTS`.

- Validate it is an `.mdx` file
- If the file does not exist, tell the user and stop

## Step 2: Read Reference Pages

Read 1-2 gold-standard pages for pattern matching. Use these reference files:

- `platform/workspace.mdx`
- `administration/environments.mdx`
- `administration/submodules.mdx`

Pick at least one reference from the same directory as the target file if available. These references define the target structure, tone, and component usage.

## Step 3: Audit Against Style Rules

Compare the target file against every rule below. Track each violation with its line number.

### Formatting Rules

- **Plain headers** — Headers use plain `##` markdown, never bold. `## **Header**` must become `## Header`.
- **No empty separators** — No empty `##` lines used as section dividers. Remove them.
- **No emojis** — Remove all emoji characters from headings, body text, and list items.
- **No em dashes** — Replace em dashes (`—`) with hyphens (`-`) for list labels or colons for explanations.
- **Mintlify callouts only** — No standalone `>` blockquote callouts. Convert to `<Note>`, `<Tip>`, or `<Warning>` components.
- **Bold-hyphen list pattern** — Bullet list items with labels use `**Bold label** - description` (hyphen separator, not colon or em dash).
- **UI paths in bold** — Navigation paths use bold: `**Settings > Integrations**`.
- **Code in backticks** — Code references, variable names, CLI commands, and filenames use backticks: `` `variable_name` ``.

### Structure Rules

- **Opening paragraph first** — The first content after frontmatter is a plain paragraph. No `## Overview` or `## Introduction` header.
- **Jump links** — Pages with more than ~4 sections include a `<Note>` with jump links as the second element: `**Jump to:** [Section](#anchor) | [Section](#anchor)`. Separator is ` | ` (pipe) or ` • ` (bullet).
- **Steps component** — Sequential procedures use `<Steps>` / `<Step title="...">` components, not numbered markdown lists.
- **Accordion component** — FAQ or troubleshooting content uses `<AccordionGroup>` / `<Accordion title="...">`.
- **Tabs component** — Multiple alternatives or parallel workflows use `<Tabs>` / `<Tab title="...">`.
- **Card navigation** — End-of-page navigation uses `<CardGroup cols={2}>` with `<Card>` items. Never use a "Related Guides" bullet list.

### Tone Rules

- **Professional and concise** — Action-oriented, no filler.
- **Imperative mood** — Instructions say "Configure..." not "You should configure...".
- **No hedging** — "Enables" not "might enable". "Sets" not "can be used to set".
- **No meta-language** — No "as mentioned above" or "as described earlier". Use specific anchor links instead.
- **Frontmatter description** — One clear sentence, no trailing period.

## Step 4: Present Findings (if `--check`)

If the `--check` flag is present, output a findings table and **stop without editing**:

```
## Audit Results — `<filename>`

| Line | Issue | Rule |
|------|-------|------|
| 12   | Bold header `## **Overview**` | Plain headers |
| 27   | Empty `##` separator | No empty separators |
| 34   | Em dash in list item | No em dashes |

**Total: N issues found**
```

If zero issues are found, report: "No issues found. File matches the Blitzy docs style guide."

Then stop — do not proceed to Step 5.

## Step 5: Apply Changes

If no `--check` flag, rewrite the file applying all rules from Step 3.

**Preserve:**
- Frontmatter fields (you may tighten the `description` for clarity)
- `<img>`, `<Frame>`, and other media components
- All factual content and information — restructure, do not remove
- Internal links and href targets
- Existing Mintlify components that already follow the rules

**Rewrite:**
- Fix every violation found in the audit
- Restructure numbered lists into `<Steps>` where they describe procedures
- Convert blockquote callouts to appropriate Mintlify components
- Add jump links `<Note>` if the page has 4+ sections and lacks them
- Convert "Related Guides" bullet lists to `<CardGroup>` navigation
- Ensure consistent spacing: one blank line between sections, no trailing whitespace

## Step 6: Verify

After editing, re-read the file and confirm:

- No emojis remain
- No em dashes remain
- No bold headers (`## **...**`) remain
- No empty `##` separators remain
- No `>` blockquote callouts remain
- All Mintlify components are properly opened and closed
- Frontmatter is valid

Report a summary of changes:

```
## Changes Applied — `<filename>`

- Removed N bold headers
- Converted N blockquote callouts to Mintlify components
- Replaced N em dashes
- Added jump links
- Converted numbered list to <Steps>
- ...

File is now compliant with the Blitzy docs style guide.
```

## Composability

After cleaning, suggest next steps:

- "Run `/review --staged` to verify the diff before committing."
- "Run `/commit` to commit the changes."

## Edge Cases

- **Already clean file** — If no issues are found, tell the user the file already matches the style guide. Do not rewrite it.
- **Non-MDX file** — Refuse to process non-`.mdx` files. Tell the user this skill is for Mintlify documentation pages only.
- **File with complex custom components** — Preserve any custom components you don't recognize. Only transform standard markdown and known Mintlify components.
- **Very long pages** — Process the entire file. Do not skip sections.
