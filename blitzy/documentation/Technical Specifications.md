# Technical Specification

# 0. Agent Action Plan

## 0.1 Intent Clarification

### 0.1.1 Core Refactoring Objective

Based on the prompt, the Blitzy platform understands that the refactoring objective is to **rebrand** a Python CLI application currently named "Mistral Vibe" (by Mistral AI) to "Blitzy Agent" (by Blitzy) and **replace the terminal color theme** with a purple-centric palette anchored on `#5B39F3`. The application is a coding agent built with Python 3.12, Textual, Pydantic, and httpx. The source lives under the `vibe/` package directory, which remains unchanged.

- **Refactoring type:** Brand identity replacement + Visual theme migration
- **Target repository:** Same repository (in-place brand string substitution and theme color replacement)
- **The `vibe/` package directory name is explicitly preserved** — only user-facing strings, CLI metadata, documentation, configuration defaults, and color definitions are affected

The refactoring goals, listed with enhanced clarity:

- **Brand string replacement:** Every user-visible occurrence of "Mistral Vibe", "mistral-vibe", "Mistral AI" (as the app author, not the API provider), "vibe" (as CLI command), "vibe-acp" (as CLI command), "~/.vibe/", "VIBE_*" (env var prefix), "vibe@mistral.ai", and "mistralai/mistral-vibe" (GitHub) must be replaced with their Blitzy equivalents per the brand mapping table
- **Theme color replacement:** The existing orange/amber welcome banner gradient and terminal-derived theme must be replaced with a cohesive purple-centric Textual CSS palette anchored on accent `#5B39F3`
- **Preserve all functional behavior:** No refactoring, no feature additions, no "while we're here" improvements — every edit is a direct brand string replacement or theme color change

Implicit requirements surfaced:

- All test assertion strings containing old branding must be updated to match the new brand, while test logic and test structure remain untouched
- The PyPI project name changes from `mistral-vibe` to `blitzy-agent`, requiring updates in update-notifier gateways, install scripts, and CI/CD workflows
- The Zed extension manifest (`distribution/zed/extension.toml`) must be updated with the new brand identity and GitHub URLs
- The GitHub Action (`action.yml`) must reflect "Blitzy Agent" in its name, description, author, and step names
- The `VIBE_HOME` environment variable override logic in `vibe/core/paths/global_paths.py` must change to `BLITZY_HOME`
- The history file greeting "Hello Vibe!" must become "Hello Blitzy!"
- The onboarding welcome screen text "Mistral Vibe" and trust dialog message must reflect "Blitzy Agent"

### 0.1.2 Technical Interpretation

This refactoring translates to the following technical transformation strategy:

- **Layer 1 — Package metadata:** Update `pyproject.toml` (project name, description, keywords, authors, URLs, script entry points), `flake.nix` description, and `action.yml`
- **Layer 2 — CLI entry points:** The script entry points change from `vibe`/`vibe-acp` to `blitzy`/`blitzy-acp` in `pyproject.toml`, but the Python module paths (`vibe.cli.entrypoint:main`, `vibe.acp.entrypoint:main`) remain identical since the `vibe/` package directory is preserved
- **Layer 3 — Configuration defaults:** `env_prefix` in `SettingsConfigDict` changes from `VIBE_` to `BLITZY_`, the default home directory changes from `~/.vibe` to `~/.blitzy`, and all user-facing config path references update accordingly
- **Layer 4 — User-facing strings:** argparse descriptions, banner text, commit signatures, user-agent strings, onboarding messages, system prompt branding, and ACP `Implementation` metadata are updated
- **Layer 5 — Visual theme:** The `app.tcss` file receives a purple-centric palette, `terminal_theme.py` adopts purple accent defaults, and the `WelcomeBanner` widget replaces the orange gradient with a purple gradient anchored on `#5B39F3`
- **Layer 6 — Documentation:** `README.md`, `CONTRIBUTING.md`, `AGENTS.md`, `CHANGELOG.md`, `docs/*.md`, and `vibe/whats_new.md` have all "Mistral Vibe" / "Mistral AI" references replaced
- **Layer 7 — Tests:** Only assertion strings containing old branding are updated; no test logic or structure changes

The brand mapping is deterministic and fully enumerated:

| Current Value | Replacement Value | Context |
|---|---|---|
| `Mistral Vibe` | `Blitzy Agent` | Display name everywhere |
| `mistral-vibe` | `blitzy-agent` | PyPI package name, CLI references, URLs |
| `Mistral AI` | `Blitzy` | Author/org name (app context only) |
| `vibe` (CLI cmd) | `blitzy` | Script entry point |
| `vibe-acp` (CLI cmd) | `blitzy-acp` | ACP script entry point |
| `~/.vibe/` | `~/.blitzy/` | Global config home directory |
| `VIBE_*` (env prefix) | `BLITZY_*` | Pydantic settings env_prefix |
| `VIBE_HOME` | `BLITZY_HOME` | Home directory override env var |
| `vibe@mistral.ai` | `agent@blitzy.com` | Commit co-author email |
| `mistralai/mistral-vibe` | `blitzy/blitzy-agent` | GitHub repository path |
| `@mistralai/mistral-vibe` | `@blitzy/blitzy-agent` | ACP Implementation name |
| `Mistral-Vibe/{version}` | `Blitzy-Agent/{version}` | HTTP User-Agent string |
| `mistral-vibe-update-notifier` | `blitzy-agent-update-notifier` | GitHub gateway User-Agent |
| `Hello Vibe!` | `Hello Blitzy!` | History file greeting |


## 0.2 Source Analysis

### 0.2.1 Comprehensive Source File Discovery

Every source file requiring modification has been identified through exhaustive `grep` searches for brand strings (`Mistral Vibe`, `mistral-vibe`, `Mistral AI`, `vibe@mistral`, `mistralai/mistral-vibe`, `Hello Vibe`, `VIBE_`, `~/.vibe`), manual review of config/path modules, theme files, test assertions, documentation, and CI/CD pipelines. The files are organized by modification category.

**Brand String Source Files (vibe/ package):**

| File | Lines Affected | Brand Strings Found |
|---|---|---|
| `vibe/__init__.py` | Line 6 | Version string — no brand change needed (version stays `2.0.2`) but reviewed for branding |
| `vibe/core/config.py` | Line 279, Line 402, Line 452 | `mistral-vibe-cli-latest` (model name — PRESERVED), `env_prefix="VIBE_"` → `BLITZY_`, comment about `VIBE_*` |
| `vibe/core/system_prompt.py` | Lines 364-369 | `Mistral Vibe` in commit signature, `vibe@mistral.ai` co-author email |
| `vibe/core/utils.py` | Line 151 | `Mistral-Vibe/{version}` user-agent string |
| `vibe/core/paths/global_paths.py` | Lines 19, 22-25, 28, 38 | `_DEFAULT_VIBE_HOME = Path.home() / ".vibe"`, `VIBE_HOME` env var name, `vibe.log` file name |
| `vibe/core/paths/config_paths.py` | Lines 26, 29, 37, 45, 53 | `cwd / ".vibe" / basename` references (local config directory) |
| `vibe/core/prompts/cli.md` | Line 1 | `Mistral Vibe, a CLI coding-agent built by Mistral AI` |
| `vibe/core/prompts/tests.md` | Line 1 | `You are Vibe` |
| `vibe/cli/entrypoint.py` | Lines 21, 77, 109 | `Mistral Vibe interactive CLI` description, `~/.vibe/agents/` help text, `vibe` reference in error msg |
| `vibe/cli/cli.py` | Lines 70-71 | `Hello Vibe!\n` history file greeting |
| `vibe/cli/textual_ui/app.py` | Lines 1224, 1252, 1264-1265 | `mistral-vibe` in update messages and PyPI gateway, `vibe --continue` resume hint |
| `vibe/cli/textual_ui/terminal_theme.py` | Entire file | Theme color logic (functional — receives purple palette changes) |
| `vibe/cli/textual_ui/app.tcss` | Entire file | Textual CSS stylesheet (uses `$variable` tokens — functional review for theme) |
| `vibe/cli/textual_ui/widgets/welcome.py` | Lines 47-48, 97 | Orange gradient `TARGET_COLORS`, `Mistral Vibe v{version}` banner text |
| `vibe/cli/update_notifier/update.py` | Line 125 | `uv tool upgrade mistral-vibe`, `brew upgrade mistral-vibe` |
| `vibe/cli/update_notifier/adapters/github_update_gateway.py` | Line 34 | `mistral-vibe-update-notifier` User-Agent |
| `vibe/acp/acp_agent_loop.py` | Lines 135, 140, 158-159 | `Mistral Vibe` in auth method, ACP `Implementation` name/title |
| `vibe/acp/entrypoint.py` | Lines 25, 45 | `Mistral Vibe in ACP mode` description, `Hello Vibe!` greeting |
| `vibe/setup/onboarding/__init__.py` | Line 54 | `Mistral Vibe CLI` in setup complete message, `"vibe"` command reference |
| `vibe/setup/onboarding/screens/welcome.py` | Line 15 | `WELCOME_HIGHLIGHT = "Mistral Vibe"` |
| `vibe/setup/onboarding/screens/api_key.py` | Lines 21, 24 | `Mistral AI Studio` (API provider — PRESERVED), `mistralai/mistral-vibe` GitHub URL |
| `vibe/setup/trusted_folders/trust_folder_dialog.py` | Line 61 | `Mistral Vibe setup` trust dialog message |
| `vibe/whats_new.md` | Line 3 | `.vibe` folder reference |

**Documentation Files:**

| File | Brand Strings Found |
|---|---|
| `README.md` | `Mistral Vibe` (multiple), `mistral-vibe` (PyPI/GitHub), `Mistral AI`, `~/.vibe/` paths, `vibe` command references |
| `CONTRIBUTING.md` | `Mistral Vibe` (multiple), `mistral-vibe` directory reference |
| `CHANGELOG.md` | `mistral-vibe` in changelog entry |
| `AGENTS.md` | No brand references found — no changes needed |
| `docs/README.md` | `Mistral Vibe` documentation title, `mistral-vibe` GitHub link |
| `docs/acp-setup.md` | `Mistral Vibe` (multiple), `vibe-acp` tool references, ACP config snippets |

**CI/CD and Distribution Files:**

| File | Brand Strings Found |
|---|---|
| `pyproject.toml` | Lines 2, 4, 8, 52-55, 70-71 — name, description, authors, URLs, scripts |
| `action.yml` | Lines 2-4, 43, 49-50 — name, description, author, step names |
| `flake.nix` | Line 2 — `Mistral Vibe!` description |
| `vibe-acp.spec` | Line 47 — `name='vibe-acp'` display name |
| `.github/CODEOWNERS` | Line 4 — `@mistralai/mistral-vibe` |
| `.github/ISSUE_TEMPLATE/bug-report.yml` | Lines 2, 16, 48-49 — `Mistral Vibe` references |
| `.github/ISSUE_TEMPLATE/config.yml` | Lines 5, 7-8 — `Mistral AI`, `mistral-vibe` URL |
| `.github/ISSUE_TEMPLATE/feature-request.yml` | Lines 2, 16, 31 — `Mistral Vibe` references |
| `.github/workflows/build-and-upload.yml` | Line 21 — `mistralai/mistral-vibe` repo check |
| `.github/workflows/release.yml` | Lines 14, 44, 50, 58 — `mistral-vibe` references |
| `distribution/zed/extension.toml` | Throughout — `mistral-vibe` id, `Mistral Vibe` name, `Mistral AI` author, URLs |
| `scripts/install.sh` | Lines 3-4, 82-85 — `Mistral Vibe` installation references |
| `scripts/prepare_release.py` | Line 26 — `mistralai/mistral-vibe.git` remote URL |

**Test Files (assertion strings only):**

| File | Lines Affected | Brand Strings Found |
|---|---|---|
| `tests/conftest.py` | Line 27 | `mistral-vibe-cli-latest` (model name — PRESERVED) |
| `tests/acp/test_initialize.py` | Lines 28, 51, 59, 65 | `@mistralai/mistral-vibe`, `Mistral Vibe` title, `Mistral Vibe Setup` label |
| `tests/acp/test_acp.py` | Line 76 | `mistral-vibe-cli-latest` (model name — PRESERVED) |
| `tests/onboarding/test_run_onboarding.py` | Line 60 | `Mistral Vibe CLI` in onboarding complete assertion |
| `tests/update_notifier/test_pypi_update_gateway.py` | Lines 29, 39, 46, 49, 51, 60, 74, 81, 96, 105, 149 | `mistral-vibe` PyPI project name, `mistral_vibe` wheel filenames |
| `tests/update_notifier/test_ui_update_notification.py` | Lines 116, 210, 405 | `mistral-vibe` in update notification messages |

### 0.2.2 Current Structure Mapping

```
Current project structure (brand-affected files highlighted):
.
├── pyproject.toml                          ← brand: name, description, authors, URLs, scripts
├── action.yml                              ← brand: name, description, author, step names
├── flake.nix                               ← brand: description
├── vibe-acp.spec                           ← brand: exe name
├── README.md                               ← brand: throughout
├── CONTRIBUTING.md                         ← brand: throughout
├── CHANGELOG.md                            ← brand: entry reference
├── AGENTS.md                               ← no brand changes needed
├── .github/
│   ├── CODEOWNERS                          ← brand: team reference
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug-report.yml                  ← brand: references
│   │   ├── config.yml                      ← brand: URLs
│   │   └── feature-request.yml             ← brand: references
│   └── workflows/
│       ├── build-and-upload.yml            ← brand: repo check
│       └── release.yml                     ← brand: PyPI/Zed references
├── distribution/
│   └── zed/
│       └── extension.toml                  ← brand: throughout
├── docs/
│   ├── README.md                           ← brand: title, links
│   └── acp-setup.md                        ← brand: references, config snippets
├── scripts/
│   ├── install.sh                          ← brand: install references
│   └── prepare_release.py                  ← brand: remote URL
├── vibe/
│   ├── __init__.py                         ← reviewed (no brand change needed)
│   ├── whats_new.md                        ← brand: .vibe reference
│   ├── core/
│   │   ├── config.py                       ← brand: env_prefix, comment
│   │   ├── system_prompt.py                ← brand: commit signature
│   │   ├── utils.py                        ← brand: user-agent
│   │   ├── paths/
│   │   │   ├── global_paths.py             ← brand: ~/.vibe, VIBE_HOME
│   │   │   └── config_paths.py             ← brand: .vibe/ local paths
│   │   └── prompts/
│   │       ├── cli.md                      ← brand: identity prompt
│   │       └── tests.md                    ← brand: identity text
│   ├── cli/
│   │   ├── entrypoint.py                   ← brand: argparse description
│   │   ├── cli.py                          ← brand: Hello Vibe greeting
│   │   ├── textual_ui/
│   │   │   ├── app.py                      ← brand: update messages, PyPI name
│   │   │   ├── app.tcss                    ← theme: CSS palette
│   │   │   ├── terminal_theme.py           ← theme: color derivation
│   │   │   └── widgets/
│   │   │       └── welcome.py              ← brand+theme: banner text, gradient colors
│   │   └── update_notifier/
│   │       ├── update.py                   ← brand: upgrade commands
│   │       └── adapters/
│   │           └── github_update_gateway.py ← brand: User-Agent
│   ├── acp/
│   │   ├── acp_agent_loop.py               ← brand: Implementation name/title, auth
│   │   └── entrypoint.py                   ← brand: argparse description, greeting
│   └── setup/
│       ├── onboarding/
│       │   ├── __init__.py                 ← brand: setup complete message
│       │   └── screens/
│       │       ├── welcome.py              ← brand: WELCOME_HIGHLIGHT
│       │       └── api_key.py              ← brand: GitHub URL
│       └── trusted_folders/
│           └── trust_folder_dialog.py      ← brand: trust dialog message
└── tests/
    ├── conftest.py                         ← model name preserved
    ├── acp/
    │   ├── test_initialize.py              ← brand: assertion strings
    │   └── test_acp.py                     ← model name preserved
    ├── onboarding/
    │   └── test_run_onboarding.py          ← brand: assertion strings
    └── update_notifier/
        ├── test_pypi_update_gateway.py     ← brand: assertion strings
        └── test_ui_update_notification.py  ← brand: assertion strings
```


## 0.3 Scope Boundaries

### 0.3.1 Exhaustively In Scope

**Source transformations (brand string replacement):**
- `vibe/core/config.py` — `env_prefix` change from `VIBE_` to `BLITZY_`, documentation comment about `VIBE_*` variables
- `vibe/core/system_prompt.py` — commit signature text (`Mistral Vibe`, `vibe@mistral.ai`)
- `vibe/core/utils.py` — user-agent string `Mistral-Vibe/{version}`
- `vibe/core/paths/global_paths.py` — `_DEFAULT_VIBE_HOME`, `VIBE_HOME` env var, `vibe.log` file name
- `vibe/core/paths/config_paths.py` — `.vibe/` local config directory references
- `vibe/core/prompts/cli.md` — system prompt identity text
- `vibe/core/prompts/tests.md` — test persona text
- `vibe/cli/entrypoint.py` — argparse description, help text with `~/.vibe/` path
- `vibe/cli/cli.py` — `Hello Vibe!` history greeting
- `vibe/cli/textual_ui/app.py` — `mistral-vibe` in update messages and PyPI gateway
- `vibe/cli/textual_ui/widgets/welcome.py` — banner text, color gradient constants
- `vibe/cli/update_notifier/update.py` — upgrade command strings
- `vibe/cli/update_notifier/adapters/github_update_gateway.py` — User-Agent header
- `vibe/acp/acp_agent_loop.py` — ACP `Implementation` name/title, auth method descriptions
- `vibe/acp/entrypoint.py` — argparse description, history greeting
- `vibe/setup/onboarding/__init__.py` — setup complete message
- `vibe/setup/onboarding/screens/welcome.py` — `WELCOME_HIGHLIGHT` constant
- `vibe/setup/onboarding/screens/api_key.py` — GitHub documentation URL
- `vibe/setup/trusted_folders/trust_folder_dialog.py` — trust dialog message
- `vibe/whats_new.md` — `.vibe` folder reference

**Theme color updates:**
- `vibe/cli/textual_ui/app.tcss` — full Textual CSS palette replacement with purple-centric colors
- `vibe/cli/textual_ui/terminal_theme.py` — theme color derivation defaults and fallback accent
- `vibe/cli/textual_ui/widgets/welcome.py` — `TARGET_COLORS` gradient and `BORDER_TARGET_COLOR` replacement with purple palette

**Test assertion updates:**
- `tests/acp/test_initialize.py` — `@mistralai/mistral-vibe`, `Mistral Vibe` title, `Mistral Vibe Setup` label assertions
- `tests/onboarding/test_run_onboarding.py` — `Mistral Vibe CLI` assertion
- `tests/update_notifier/test_pypi_update_gateway.py` — `mistral-vibe` project name, `mistral_vibe` wheel filename assertions
- `tests/update_notifier/test_ui_update_notification.py` — `mistral-vibe` in update notification assertions

**Configuration and packaging updates:**
- `pyproject.toml` — name, description, keywords, authors, URLs, scripts
- `action.yml` — GitHub Action name, description, author, step names
- `flake.nix` — description string
- `vibe-acp.spec` — exe display name

**Documentation updates:**
- `README.md` — all `Mistral Vibe`, `mistral-vibe`, `Mistral AI`, `~/.vibe/`, `vibe` command references
- `CONTRIBUTING.md` — all brand references
- `CHANGELOG.md` — `mistral-vibe` entry reference
- `docs/README.md` — title and link references
- `docs/acp-setup.md` — brand references and ACP config snippet examples

**CI/CD and distribution updates:**
- `.github/CODEOWNERS` — team reference
- `.github/ISSUE_TEMPLATE/bug-report.yml` — brand references
- `.github/ISSUE_TEMPLATE/config.yml` — URLs and brand references
- `.github/ISSUE_TEMPLATE/feature-request.yml` — brand references
- `.github/workflows/build-and-upload.yml` — repository check string
- `.github/workflows/release.yml` — PyPI and Zed references
- `distribution/zed/extension.toml` — id, name, author, URLs, agent server names
- `scripts/install.sh` — installation references
- `scripts/prepare_release.py` — remote URL

**Import path corrections:**
- No import path changes required — the `vibe/` package directory is preserved
- Only string-literal brand replacements within existing files

### 0.3.2 Explicitly Out of Scope

The following are explicitly excluded from modification per the user's preservation requirements:

- **Internal Python identifiers:** `VibeConfig`, `VibeApp`, `AgentLoop`, `VibeAcpAgentLoop`, or any class/function/variable name
- **The `vibe/` package directory name** — the directory remains `vibe/`, not renamed
- **Internal constants:** `VIBE_ROOT` (defined in `vibe/__init__.py`), `VIBE_STOP_EVENT_TAG`, `VIBE_WARNING_TAG` (defined in `vibe/core/utils.py`)
- **External API provider references:**
  - `"mistral"` as a provider name in `DEFAULT_PROVIDERS` (refers to the Mistral API service)
  - `MISTRAL_API_KEY` environment variable (external API credential)
  - `api.mistral.ai` and `codestral.mistral.ai` (external API endpoints)
  - `Mistral AI Studio` label in `vibe/setup/onboarding/screens/api_key.py` line 21 (refers to the external console)
  - The `mistralai` Python SDK package dependency
- **LLM model names:** `mistral-vibe-cli-latest`, `devstral-2`, `devstral-small`, `devstral-small-latest`, `devstral` (these are model identifiers, not app branding)
- **Functional behavior:** No tool, agent, middleware, backend, or protocol behavior changes
- **Config file formats:** TOML config format, session log format, ACP protocol schema unchanged
- **Test logic or test structure:** Only assertion strings with old branding are updated
- **The `tests/conftest.py` base config model name** `mistral-vibe-cli-latest` — this is a model name, not app branding
- **The `tests/acp/test_acp.py` model name check** — `mistral-vibe-cli-latest` is a model identifier


## 0.4 Target Design

### 0.4.1 Refactored Structure Planning

The target structure preserves the identical directory layout — no files are moved, created, or deleted. Only in-place content modifications are applied. The complete list of files with their modification nature:

```
Target (all files are in-place updates — no structural changes):
.
├── pyproject.toml                          ← UPDATE: name→blitzy-agent, scripts→blitzy/blitzy-acp
├── action.yml                              ← UPDATE: name→Blitzy Agent, author→Blitzy
├── flake.nix                               ← UPDATE: description→Blitzy Agent
├── vibe-acp.spec                           ← UPDATE: name→blitzy-acp
├── README.md                               ← UPDATE: all brand strings
├── CONTRIBUTING.md                         ← UPDATE: all brand strings
├── CHANGELOG.md                            ← UPDATE: brand string in entry
├── .github/
│   ├── CODEOWNERS                          ← UPDATE: team→@blitzy/blitzy-agent
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug-report.yml                  ← UPDATE: brand strings
│   │   ├── config.yml                      ← UPDATE: URLs and brand strings
│   │   └── feature-request.yml             ← UPDATE: brand strings
│   └── workflows/
│       ├── build-and-upload.yml            ← UPDATE: repo check string
│       └── release.yml                     ← UPDATE: PyPI/Zed references
├── distribution/
│   └── zed/
│       └── extension.toml                  ← UPDATE: id, name, author, all URLs
├── docs/
│   ├── README.md                           ← UPDATE: brand strings and links
│   └── acp-setup.md                        ← UPDATE: brand strings and config examples
├── scripts/
│   ├── install.sh                          ← UPDATE: brand and install references
│   └── prepare_release.py                  ← UPDATE: remote URL
├── vibe/
│   ├── whats_new.md                        ← UPDATE: .vibe → .blitzy
│   ├── core/
│   │   ├── config.py                       ← UPDATE: env_prefix VIBE_→BLITZY_
│   │   ├── system_prompt.py                ← UPDATE: commit signature branding
│   │   ├── utils.py                        ← UPDATE: user-agent string
│   │   ├── paths/
│   │   │   ├── global_paths.py             ← UPDATE: ~/.vibe→~/.blitzy, VIBE_HOME→BLITZY_HOME
│   │   │   └── config_paths.py             ← UPDATE: .vibe/→.blitzy/ local paths
│   │   └── prompts/
│   │       ├── cli.md                      ← UPDATE: identity prompt text
│   │       └── tests.md                    ← UPDATE: identity persona text
│   ├── cli/
│   │   ├── entrypoint.py                   ← UPDATE: argparse description, help text
│   │   ├── cli.py                          ← UPDATE: Hello Vibe!→Hello Blitzy!
│   │   ├── textual_ui/
│   │   │   ├── app.py                      ← UPDATE: package name in update messages
│   │   │   ├── app.tcss                    ← UPDATE: purple-centric palette
│   │   │   ├── terminal_theme.py           ← UPDATE: accent color defaults
│   │   │   └── widgets/
│   │   │       └── welcome.py              ← UPDATE: banner text + purple gradient
│   │   └── update_notifier/
│   │       ├── update.py                   ← UPDATE: upgrade command strings
│   │       └── adapters/
│   │           └── github_update_gateway.py ← UPDATE: User-Agent
│   ├── acp/
│   │   ├── acp_agent_loop.py               ← UPDATE: Implementation name/title
│   │   └── entrypoint.py                   ← UPDATE: argparse description, greeting
│   └── setup/
│       ├── onboarding/
│       │   ├── __init__.py                 ← UPDATE: setup complete message
│       │   └── screens/
│       │       ├── welcome.py              ← UPDATE: WELCOME_HIGHLIGHT
│       │       └── api_key.py              ← UPDATE: GitHub URL only
│       └── trusted_folders/
│           └── trust_folder_dialog.py      ← UPDATE: trust dialog message
└── tests/
    ├── acp/
    │   └── test_initialize.py              ← UPDATE: assertion strings
    ├── onboarding/
    │   └── test_run_onboarding.py          ← UPDATE: assertion strings
    └── update_notifier/
        ├── test_pypi_update_gateway.py     ← UPDATE: assertion strings
        └── test_ui_update_notification.py  ← UPDATE: assertion strings
```

### 0.4.2 Design Pattern Applications

This rebranding exercise applies two core design patterns:

- **Find-and-replace with exclusion boundaries:** Every substitution follows the brand mapping table with strict exclusion rules for preserved identifiers (model names, API provider references, internal constants). The approach is deterministic — each old string maps to exactly one new string
- **Minimal Change Clause compliance:** Every edit is validated as either a direct brand string replacement or a theme color change. No refactoring, feature additions, or opportunistic improvements are permitted

### 0.4.3 Theme Specification

The purple-centric Textual CSS palette is anchored on primary accent `#5B39F3` with the following derived values:

| Token | Value | Purpose |
|---|---|---|
| Primary accent | `#5B39F3` | Primary interactive elements, focus ring |
| Accent hover | `#7C5DF5` | Lighter variant for hover states |
| Accent active | `#4A2DD4` | Darker variant for active/pressed states |
| Muted/secondary | `#8B7FC7` | Desaturated purple for secondary text |
| Border | Muted purple derived from `#5B39F3` at reduced opacity | Border accents |
| Text on accent | `#FFFFFF` | White text on purple backgrounds |
| Surface/background | Dark neutrals with subtle purple undertone | Panel and surface backgrounds |
| Error/warning/success | Preserved existing semantic colors | Unless they clash with purple palette |

The `WelcomeBanner` widget gradient colors will shift from the current orange palette (`#FFD800` → `#E10500`) to a purple-centric gradient derived from `#5B39F3` (e.g., `#7C5DF5` → `#5B39F3` → `#4A2DD4` → `#3A1FB5` → `#2A0F96`).

### 0.4.4 User Interface Design

The rebranding affects the following user-facing surfaces:

- **Welcome banner:** Displays "Blitzy Agent v{version}" with purple gradient animation instead of "Mistral Vibe v{version}" with orange gradient
- **Onboarding welcome screen:** The `WELCOME_HIGHLIGHT` text changes from "Mistral Vibe" to "Blitzy Agent"
- **Trust folder dialog:** Message changes from "Files that can modify your Mistral Vibe setup" to "Files that can modify your Blitzy Agent setup"
- **CLI help text:** `blitzy --help` outputs description containing "Blitzy Agent" and `~/.blitzy/agents/` path
- **ACP initialization:** The `agent_info` Implementation reports `name="@blitzy/blitzy-agent"`, `title="Blitzy Agent"`
- **Commit signature:** Reads `Generated by Blitzy Agent.` and `Co-Authored-By: Blitzy Agent <agent@blitzy.com>`
- **Update notifications:** Reference `blitzy-agent` package name and `uv tool upgrade blitzy-agent` command
- **Session resume hint:** Prints `blitzy --continue` and `blitzy --resume {session_id}`


## 0.5 Transformation Mapping

### 0.5.1 File-by-File Transformation Plan

Every target file is mapped to its source with specific key changes. All transformations are UPDATE mode (in-place modification of existing files). The entire refactor executes in ONE phase.

**Package Metadata and Build Configuration:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `pyproject.toml` | UPDATE | `pyproject.toml` | `name = "mistral-vibe"` → `"blitzy-agent"`, `description` → `"Minimal CLI coding agent by Blitzy"`, `authors` → `[{ name = "Blitzy" }]`, `keywords` remove `"mistral"` add `"blitzy"`, `Homepage`/`Repository`/`Issues`/`Documentation` URLs → `blitzy/blitzy-agent`, `scripts` → `blitzy = "vibe.cli.entrypoint:main"` and `blitzy-acp = "vibe.acp.entrypoint:main"` |
| `action.yml` | UPDATE | `action.yml` | `name: Mistral Vibe` → `Blitzy Agent`, `description` → `"Download, install, and run Blitzy Agent"`, `author: Mistral AI` → `Blitzy`, step names `Install Mistral Vibe` → `Install Blitzy Agent`, `Run Mistral Vibe` → `Run Blitzy Agent`, `id: run-mistral-vibe` → `run-blitzy-agent`, `vibe \` → `blitzy \` in run command |
| `flake.nix` | UPDATE | `flake.nix` | `description = "Mistral Vibe!"` → `"Blitzy Agent!"`, `pythonSet.mistral-vibe` → `pythonSet.blitzy-agent` |
| `vibe-acp.spec` | UPDATE | `vibe-acp.spec` | `name='vibe-acp'` → `'blitzy-acp'` |

**Core Runtime — Brand Strings:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `vibe/core/config.py` | UPDATE | `vibe/core/config.py` | Line 402: `env_prefix="VIBE_"` → `env_prefix="BLITZY_"`, Line 452: update comment `VIBE_*` → `BLITZY_*` |
| `vibe/core/system_prompt.py` | UPDATE | `vibe/core/system_prompt.py` | Line 364: `"generated by Mistral Vibe"` → `"generated by Blitzy Agent"`, Line 368: `"Generated by Mistral Vibe.\n"` → `"Generated by Blitzy Agent.\n"`, Line 369: `"Co-Authored-By: Mistral Vibe <vibe@mistral.ai>\n"` → `"Co-Authored-By: Blitzy Agent <agent@blitzy.com>\n"` |
| `vibe/core/utils.py` | UPDATE | `vibe/core/utils.py` | Line 151: `f"Mistral-Vibe/{__version__}"` → `f"Blitzy-Agent/{__version__}"` |
| `vibe/core/paths/global_paths.py` | UPDATE | `vibe/core/paths/global_paths.py` | Line 19: `Path.home() / ".vibe"` → `Path.home() / ".blitzy"`, Line 23: `os.getenv("VIBE_HOME")` → `os.getenv("BLITZY_HOME")`, Line 38: `"vibe.log"` → `"blitzy.log"` |
| `vibe/core/paths/config_paths.py` | UPDATE | `vibe/core/paths/config_paths.py` | Lines 26, 29: `cwd / ".vibe" / basename` → `cwd / ".blitzy" / basename`, Lines 37, 45, 53: `dir / ".vibe" / "tools"`, `"skills"`, `"agents"` → `dir / ".blitzy" / ...` |

**Core Runtime — Prompt Templates:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `vibe/core/prompts/cli.md` | UPDATE | `vibe/core/prompts/cli.md` | Line 1: `"Mistral Vibe, a CLI coding-agent built by Mistral AI"` → `"Blitzy Agent, a CLI coding-agent built by Blitzy"` |
| `vibe/core/prompts/tests.md` | UPDATE | `vibe/core/prompts/tests.md` | Line 1: `"You are Vibe"` → `"You are Blitzy Agent"` |

**CLI Entry Points and UI:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `vibe/cli/entrypoint.py` | UPDATE | `vibe/cli/entrypoint.py` | Line 21: `"Run the Mistral Vibe interactive CLI"` → `"Run the Blitzy Agent interactive CLI"`, Line 77: `~/.vibe/agents/` → `~/.blitzy/agents/`, Line 109: `"vibe from"` → `"blitzy from"` |
| `vibe/cli/cli.py` | UPDATE | `vibe/cli/cli.py` | Line 70: `"Hello Vibe!\n"` → `"Hello Blitzy!\n"` |
| `vibe/cli/textual_ui/app.py` | UPDATE | `vibe/cli/textual_ui/app.py` | Line 1224: `"mistral-vibe"` → `"blitzy-agent"` in update message, Line 1252: `project_name="mistral-vibe"` → `project_name="blitzy-agent"`, Lines 1264-1265: `"vibe --continue"` → `"blitzy --continue"`, `"vibe --resume"` → `"blitzy --resume"` |
| `vibe/cli/textual_ui/app.tcss` | UPDATE | `vibe/cli/textual_ui/app.tcss` | Replace the `WelcomeBanner` border color and add purple-centric theme variable overrides. The `app.tcss` file uses Textual `$variable` tokens — ensure the theme definition feeds the purple palette through Textual's theming system |
| `vibe/cli/textual_ui/terminal_theme.py` | UPDATE | `vibe/cli/textual_ui/terminal_theme.py` | Update the `capture_terminal_theme()` function's fallback/default `Theme` construction: change `accent=colors.magenta or fg` to use `#5B39F3` as the accent color, and `primary=colors.blue or fg` to reflect the purple primary. Ensure the default theme aligns with the purple palette |
| `vibe/cli/textual_ui/widgets/welcome.py` | UPDATE | `vibe/cli/textual_ui/widgets/welcome.py` | Line 47: `TARGET_COLORS` replace orange gradient `("#FFD800", "#FFAF00", "#FF8205", "#FA500F", "#E10500")` → purple gradient (e.g., `("#7C5DF5", "#6B4AF0", "#5B39F3", "#4A2DD4", "#3A1FB5")`), Line 48: `BORDER_TARGET_COLOR = "#b05800"` → a purple border color (e.g., `"#5B39F3"`), Line 97: `"Mistral Vibe v{version}"` → `"Blitzy Agent v{version}"` |

**Update Notifier:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `vibe/cli/update_notifier/update.py` | UPDATE | `vibe/cli/update_notifier/update.py` | Line 125: `["uv tool upgrade mistral-vibe", "brew upgrade mistral-vibe"]` → `["uv tool upgrade blitzy-agent", "brew upgrade blitzy-agent"]` |
| `vibe/cli/update_notifier/adapters/github_update_gateway.py` | UPDATE | `vibe/cli/update_notifier/adapters/github_update_gateway.py` | Line 34: `"mistral-vibe-update-notifier"` → `"blitzy-agent-update-notifier"` |

**ACP Layer:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `vibe/acp/acp_agent_loop.py` | UPDATE | `vibe/acp/acp_agent_loop.py` | Line 135: `"Register your API Key inside Mistral Vibe"` → `"Register your API Key inside Blitzy Agent"`, Line 140: `"label": "Mistral Vibe Setup"` → `"label": "Blitzy Agent Setup"`, Line 158: `name="@mistralai/mistral-vibe"` → `name="@blitzy/blitzy-agent"`, Line 159: `title="Mistral Vibe"` → `title="Blitzy Agent"` |
| `vibe/acp/entrypoint.py` | UPDATE | `vibe/acp/entrypoint.py` | Line 25: `"Run Mistral Vibe in ACP mode"` → `"Run Blitzy Agent in ACP mode"`, Line 45: `"Hello Vibe!\n"` → `"Hello Blitzy!\n"` |

**Setup and Onboarding:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `vibe/setup/onboarding/__init__.py` | UPDATE | `vibe/setup/onboarding/__init__.py` | Line 54: `'Run "vibe" to start using the Mistral Vibe CLI.'` → `'Run "blitzy" to start using the Blitzy Agent CLI.'` |
| `vibe/setup/onboarding/screens/welcome.py` | UPDATE | `vibe/setup/onboarding/screens/welcome.py` | Line 15: `WELCOME_HIGHLIGHT = "Mistral Vibe"` → `WELCOME_HIGHLIGHT = "Blitzy Agent"` |
| `vibe/setup/onboarding/screens/api_key.py` | UPDATE | `vibe/setup/onboarding/screens/api_key.py` | Line 24: `"https://github.com/mistralai/mistral-vibe?tab=readme-ov-file#configuration"` → `"https://github.com/blitzy/blitzy-agent?tab=readme-ov-file#configuration"` |
| `vibe/setup/trusted_folders/trust_folder_dialog.py` | UPDATE | `vibe/setup/trusted_folders/trust_folder_dialog.py` | Line 61: `"Files that can modify your Mistral Vibe setup"` → `"Files that can modify your Blitzy Agent setup"` |

**Release Notes:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `vibe/whats_new.md` | UPDATE | `vibe/whats_new.md` | Line 3: `".vibe folder"` → `".blitzy folder"` |

**Documentation:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `README.md` | UPDATE | `README.md` | All `Mistral Vibe` → `Blitzy Agent`, `mistral-vibe` → `blitzy-agent`, `Mistral AI` (as author) → `Blitzy`, `mistralai/mistral-vibe` → `blitzy/blitzy-agent`, `~/.vibe/` → `~/.blitzy/`, `vibe` (CLI cmd) → `blitzy`, `vibe-acp` → `blitzy-acp`, preserve `Mistral's models` / `api.mistral.ai` references |
| `CONTRIBUTING.md` | UPDATE | `CONTRIBUTING.md` | All `Mistral Vibe` → `Blitzy Agent`, `mistral-vibe` directory → `blitzy-agent` |
| `CHANGELOG.md` | UPDATE | `CHANGELOG.md` | `mistral-vibe` → `blitzy-agent` in applicable entries |
| `docs/README.md` | UPDATE | `docs/README.md` | `Mistral Vibe` → `Blitzy Agent`, URL references |
| `docs/acp-setup.md` | UPDATE | `docs/acp-setup.md` | `Mistral Vibe` → `Blitzy Agent`, `vibe-acp` → `blitzy-acp`, config snippet names `"Mistral Vibe"` → `"Blitzy Agent"`, `mistral-vibe` Zed extension → `blitzy-agent` |

**CI/CD and Distribution:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `.github/CODEOWNERS` | UPDATE | `.github/CODEOWNERS` | `@mistralai/mistral-vibe` → `@blitzy/blitzy-agent` |
| `.github/ISSUE_TEMPLATE/bug-report.yml` | UPDATE | `.github/ISSUE_TEMPLATE/bug-report.yml` | `Mistral Vibe` → `Blitzy Agent`, `mistral-vibe` → `blitzy-agent` |
| `.github/ISSUE_TEMPLATE/config.yml` | UPDATE | `.github/ISSUE_TEMPLATE/config.yml` | `Mistral AI` → `Blitzy`, `docs.mistral.ai/mistral-vibe` → `docs.blitzy.com/blitzy-agent`, `Mistral Vibe` → `Blitzy Agent` |
| `.github/ISSUE_TEMPLATE/feature-request.yml` | UPDATE | `.github/ISSUE_TEMPLATE/feature-request.yml` | `Mistral Vibe` → `Blitzy Agent` |
| `.github/workflows/build-and-upload.yml` | UPDATE | `.github/workflows/build-and-upload.yml` | `"mistralai/mistral-vibe"` → `"blitzy/blitzy-agent"` |
| `.github/workflows/release.yml` | UPDATE | `.github/workflows/release.yml` | `mistral-vibe` → `blitzy-agent` throughout |
| `distribution/zed/extension.toml` | UPDATE | `distribution/zed/extension.toml` | `id = "mistral-vibe"` → `"blitzy-agent"`, `name = "Mistral Vibe"` → `"Blitzy Agent"`, `authors = ["Mistral AI"]` → `["Blitzy"]`, repository URL, all `agent_servers.mistral-vibe` section keys → `agent_servers.blitzy-agent`, archive URLs → `blitzy/blitzy-agent`, `icon = "./icons/mistral_vibe.svg"` → update to appropriate icon reference |
| `scripts/install.sh` | UPDATE | `scripts/install.sh` | `Mistral Vibe` → `Blitzy Agent`, `mistral-vibe` → `blitzy-agent`, `vibe, vibe-acp` → `blitzy, blitzy-acp` |
| `scripts/prepare_release.py` | UPDATE | `scripts/prepare_release.py` | `"git@github.com:mistralai/mistral-vibe.git"` → `"git@github.com:blitzy/blitzy-agent.git"` |

**Test Assertion Strings:**

| Target File | Transformation | Source File | Key Changes |
|---|---|---|---|
| `tests/acp/test_initialize.py` | UPDATE | `tests/acp/test_initialize.py` | Line 28, 51: `name="@mistralai/mistral-vibe", title="Mistral Vibe"` → `name="@blitzy/blitzy-agent", title="Blitzy Agent"`, Line 59: `"Register your API Key inside Mistral Vibe"` → `"... Blitzy Agent"`, Line 65: `"Mistral Vibe Setup"` → `"Blitzy Agent Setup"` |
| `tests/onboarding/test_run_onboarding.py` | UPDATE | `tests/onboarding/test_run_onboarding.py` | Line 60: `"Mistral Vibe CLI"` → `"Blitzy Agent CLI"`, `"vibe"` → `"blitzy"` |
| `tests/update_notifier/test_pypi_update_gateway.py` | UPDATE | `tests/update_notifier/test_pypi_update_gateway.py` | Lines 29, 60, 81, 105, 149: `project_name="mistral-vibe"` → `"blitzy-agent"`, Line 39: `"/simple/mistral-vibe/"` → `"/simple/blitzy-agent/"`, Lines 46, 49, 51, 74, 96: `"mistral_vibe-"` → `"blitzy_agent-"` in wheel filenames |
| `tests/update_notifier/test_ui_update_notification.py` | UPDATE | `tests/update_notifier/test_ui_update_notification.py` | Lines 116, 210, 405: `"mistral-vibe"` → `"blitzy-agent"` in expected notification messages |

### 0.5.2 Cross-File Dependencies

**Import statement updates:** None required — all Python import paths remain unchanged since the `vibe/` package directory is preserved and no module or class names are altered.

**Configuration updates for new structure:**
- The `env_prefix` change in `vibe/core/config.py` from `VIBE_` to `BLITZY_` means any user currently setting `VIBE_ACTIVE_MODEL=devstral-2` will need to use `BLITZY_ACTIVE_MODEL=devstral-2`
- The home directory change from `~/.vibe/` to `~/.blitzy/` means existing user configurations will need manual migration (existing `~/.vibe/config.toml` will not be auto-discovered)
- The `VIBE_HOME` override environment variable changes to `BLITZY_HOME`

**Test file impact:** Only assertion strings are updated to match the new brand values. No test imports, fixtures, mocking logic, or test structure changes are needed.

### 0.5.3 Wildcard Pattern Summary

All wildcard patterns use trailing patterns only:

- `vibe/**/*.py` — scan all Python source files for user-facing brand strings
- `vibe/core/prompts/*.md` — update prompt template branding
- `tests/**/*.py` — update hardcoded brand strings in test assertions ONLY
- `.github/ISSUE_TEMPLATE/*.yml` — update issue template brand references
- `.github/workflows/*.yml` — update CI/CD workflow brand references
- `docs/**/*.md` — update documentation brand references
- `scripts/*` — update script brand references

### 0.5.4 One-Phase Execution

The entire refactor executes in ONE phase. All files listed above are modified simultaneously in a single pass. No multi-phase or staged rollout is required — this is a pure find-and-replace operation with theme color substitution, and all changes are independent at the code level (no file depends on the brand value of another file at import time).


## 0.6 Dependency Inventory

### 0.6.1 Key Packages

All packages are existing dependencies — no new packages are added or removed. The versions below are taken directly from `pyproject.toml` and the installed environment.

| Registry | Package | Version | Purpose |
|---|---|---|---|
| PyPI | `agent-client-protocol` | `==0.7.1` | ACP agent protocol for editor integration |
| PyPI | `anyio` | `>=4.12.0` | Async I/O abstraction layer |
| PyPI | `httpx` | `>=0.28.1` | HTTP client for API calls and update checks |
| PyPI | `mcp` | `>=1.14.0` | MCP server integration |
| PyPI | `mistralai` | `==1.9.11` | Mistral AI Python SDK (PRESERVED — external dependency) |
| PyPI | `pexpect` | `>=4.9.0` | Terminal interaction |
| PyPI | `packaging` | `>=24.1` | Version parsing for update notifier |
| PyPI | `pydantic` | `>=2.12.4` | Data validation and settings management |
| PyPI | `pydantic-settings` | `>=2.12.0` | Settings configuration with env var support |
| PyPI | `pyyaml` | `>=6.0.0` | YAML parsing |
| PyPI | `python-dotenv` | `>=1.0.0` | `.env` file loading |
| PyPI | `rich` | `>=14.0.0` | Terminal formatting |
| PyPI | `textual` | `>=1.0.0` | TUI framework for CLI interface |
| PyPI | `tomli-w` | `>=1.2.0` | TOML writing for config persistence |
| PyPI | `watchfiles` | `>=1.1.1` | File watching for auto-completion indexer |
| PyPI | `pyperclip` | `>=1.11.0` | Clipboard integration |
| PyPI | `textual-speedups` | `>=0.2.1` | Textual performance optimizations |
| PyPI | `tree-sitter` | `>=0.25.2` | Code parsing |
| PyPI | `tree-sitter-bash` | `>=0.25.1` | Bash grammar for tree-sitter |
| PyPI | `hatchling` | build-time | Build backend |
| PyPI | `hatch-vcs` | build-time | Version control integration for hatchling |
| PyPI | `editables` | build-time | Editable install support |

**Dev dependencies (from dependency-groups):**

| Registry | Package | Version | Purpose |
|---|---|---|---|
| PyPI | `ruff` | `>=0.14.5` | Linter and formatter |
| PyPI | `pyright` | `>=1.1.403` | Type checking |
| PyPI | `pytest` | `>=8.3.5` | Test framework |
| PyPI | `pytest-asyncio` | `>=1.2.0` | Async test support |
| PyPI | `pytest-timeout` | `>=2.4.0` | Test timeout management |
| PyPI | `pytest-textual-snapshot` | `>=1.1.0` | Textual UI snapshot testing |
| PyPI | `pytest-xdist` | `>=3.8.0` | Parallel test execution |
| PyPI | `respx` | `>=0.22.0` | HTTP mocking for httpx |
| PyPI | `debugpy` | `>=1.8.19` | Debug adapter |

### 0.6.2 Dependency Updates

**No dependency additions or removals** are required for this rebranding exercise. All existing dependencies remain at their current versions.

**Import Refactoring:** No import changes are needed since the `vibe/` package directory is preserved and no module names change.

**External Reference Updates:**

The following configuration and build files require brand string updates (not dependency changes):

- `pyproject.toml` — project name, description, keywords, authors, URLs, script entry points (module paths remain unchanged: `vibe.cli.entrypoint:main`, `vibe.acp.entrypoint:main`)
- `flake.nix` — description string, package reference
- `action.yml` — GitHub Action metadata only (no dependency changes)
- `.github/workflows/release.yml` — PyPI project reference and Zed extension name
- `distribution/zed/extension.toml` — extension metadata and download URLs

### 0.6.3 Environment Variable Migration

The `env_prefix` change from `VIBE_` to `BLITZY_` affects the following environment variables that users may have configured:

| Old Variable | New Variable | Purpose |
|---|---|---|
| `VIBE_ACTIVE_MODEL` | `BLITZY_ACTIVE_MODEL` | Override active model |
| `VIBE_HOME` | `BLITZY_HOME` | Override home directory location |
| `VIBE_*` (any) | `BLITZY_*` | Any Pydantic settings override |

**Preserved environment variables (NOT renamed):**
- `MISTRAL_API_KEY` — external API credential, explicitly preserved
- `DEBUG_MODE` — debugpy activation, not branded


## 0.7 Refactoring Rules

### 0.7.1 User-Specified Rules and Requirements

The following rules are explicitly mandated by the user and must be enforced on every edit:

- **Minimal Change Clause:** Every edit MUST be a direct brand string replacement or theme color change. No refactoring, no feature additions, no "while we're here" improvements
- **Internal Python identifiers are immutable:** `VibeConfig`, `VibeApp`, `AgentLoop`, `VibeAcpAgentLoop`, and all other class/function/variable names MUST NOT be modified
- **The `vibe/` package directory name is preserved:** The directory remains `vibe/`, not renamed to `blitzy/`
- **Internal constants are preserved:** `VIBE_ROOT` (in `vibe/__init__.py`), `VIBE_STOP_EVENT_TAG`, `VIBE_WARNING_TAG` (in `vibe/core/utils.py`) MUST NOT be modified
- **External Mistral API references are preserved:**
  - Provider name `"mistral"` in config (refers to the Mistral API service, not the app)
  - `MISTRAL_API_KEY` environment variable
  - `api.mistral.ai` and `codestral.mistral.ai` endpoints
  - The `mistralai` Python SDK package dependency
  - `Mistral AI Studio` label in the onboarding API key screen (external service reference)
- **LLM model names are preserved:** `mistral-vibe-cli-latest`, `devstral-2`, `devstral-small`, `devstral-small-latest` are model identifiers, not app branding
- **All existing functionality must be preserved:** No behavioral changes to any tool, agent, middleware, backend, or protocol
- **Config file format and session log format are unchanged**
- **Test logic and test structure are unchanged:** Only assertion strings containing old branding are updated

### 0.7.2 Special Instructions and Constraints

**Disambiguation Protocol for "Mistral" References:**

When encountering the word "Mistral" in the codebase, apply the following decision tree:

- If the context refers to the **application brand** (e.g., "Mistral Vibe", "built by Mistral AI" in app description) → **REPLACE** with Blitzy equivalent
- If the context refers to the **external Mistral API service** (e.g., `api.mistral.ai`, `MISTRAL_API_KEY`, `mistralai` SDK, provider `name="mistral"`, `Mistral AI Studio` console) → **PRESERVE** unchanged
- If the context refers to a **model name** (e.g., `mistral-vibe-cli-latest`) → **PRESERVE** unchanged
- If the context is in a **GitHub repository path** (e.g., `mistralai/mistral-vibe`) → **REPLACE** with `blitzy/blitzy-agent`

**Theme Color Constraints:**

- Primary accent `#5B39F3` is the anchor color — all derived colors must be cohesive with this purple
- Semantic colors (error/warning/success) should be preserved from the existing theme unless they visually clash with the purple palette
- The Textual CSS file (`app.tcss`) uses `$variable` tokens from the Textual theming system — the purple palette is injected through the `Theme` object, not hardcoded in CSS
- The `WelcomeBanner` widget uses direct hex color strings for its gradient animation — these must be replaced with purple gradient values

**Validation Framework (Automated Checks):**

The following commands must pass after all changes are applied:

- `grep -ri "mistral.vibe\|mistral-vibe\|Mistral Vibe" vibe/ tests/ docs/ *.md *.yml` returns zero matches (excluding lines containing `api.mistral.ai`, `codestral.mistral.ai`, `mistralai` SDK, or `MISTRAL_API_KEY`)
- `grep -ri "Mistral AI" vibe/ tests/ docs/ *.md *.yml` returns zero matches (excluding lines referencing the external Mistral API provider configuration)
- `uv run pytest` — all tests pass
- `uv run ruff check` — zero lint errors
- `uv run ruff format --check` — zero format violations
- `blitzy --help` outputs help text containing "Blitzy Agent" and zero mentions of "Mistral Vibe"
- `blitzy-acp` launches without error

**Manual Verification Criteria:**

- Welcome screen displays "Blitzy Agent" with purple-themed UI
- Accent colors in TUI visually match `#5B39F3` purple palette
- Commit signature reads `Blitzy Agent <agent@blitzy.com>`

### 0.7.3 Boundary Edge Cases

The following specific cases require careful handling:

| Location | String | Decision | Rationale |
|---|---|---|---|
| `vibe/core/config.py:279` | `name="mistral-vibe-cli-latest"` | PRESERVE | This is a model name, not app branding |
| `vibe/setup/onboarding/screens/api_key.py:21` | `"Mistral AI Studio"` | PRESERVE | Refers to the external API console |
| `vibe/core/config.py:264-269` | `name="mistral"`, `api_base="https://api.mistral.ai/v1"` | PRESERVE | External API provider config |
| `tests/conftest.py:27` | `"name": "mistral-vibe-cli-latest"` | PRESERVE | Model name in test config |
| `tests/acp/test_acp.py:76` | `"mistral-vibe-cli-latest"` | PRESERVE | Model name assertion |
| `vibe/core/utils.py:24` | `VIBE_STOP_EVENT_TAG` | PRESERVE | Internal constant |
| `vibe/core/utils.py:25` | `VIBE_WARNING_TAG` | PRESERVE | Internal constant |
| `vibe/__init__.py:5` | `VIBE_ROOT` | PRESERVE | Internal path constant |
| `vibe/core/config.py:302` | `class VibeConfig` | PRESERVE | Internal class name |
| `vibe/core/paths/global_paths.py:28` | `VIBE_HOME = GlobalPath(...)` | Variable name PRESERVE, but the resolver function must return `~/.blitzy` and check `BLITZY_HOME` env var | The Python variable name `VIBE_HOME` is an internal identifier, but the resolved path and env var it reads must change |


## 0.8 References

### 0.8.1 Files and Folders Searched

The following files and folders were retrieved and analyzed to derive the conclusions in this Agent Action Plan:

**Root-level files read:**
- `pyproject.toml` — package metadata, dependencies, build config, scripts, tool config
- `action.yml` — GitHub Action definition
- `vibe-acp.spec` — PyInstaller spec for ACP binary
- `flake.nix` — Nix flake definition (brand reference confirmed via grep)
- `.python-version` — Python 3.12 confirmed
- `.github/CODEOWNERS` — team ownership

**Source code files read in full:**
- `vibe/__init__.py` — `VIBE_ROOT` and `__version__` constants
- `vibe/core/config.py` — `VibeConfig`, `SettingsConfigDict`, `env_prefix`, provider/model defaults
- `vibe/core/system_prompt.py` — commit signature, system prompt assembly, `ProjectContextProvider`
- `vibe/core/utils.py` — user-agent string, internal constants (`VIBE_STOP_EVENT_TAG`, `VIBE_WARNING_TAG`)
- `vibe/core/paths/global_paths.py` — `_DEFAULT_VIBE_HOME`, `VIBE_HOME` env var, all global path definitions
- `vibe/core/paths/config_paths.py` — local `.vibe/` config directory resolution
- `vibe/core/prompts/cli.md` — CLI system prompt template
- `vibe/core/prompts/tests.md` — test persona prompt
- `vibe/cli/entrypoint.py` — CLI argparse description and trusted folder flow
- `vibe/cli/cli.py` — CLI bootstrap, `Hello Vibe!` greeting, session loading
- `vibe/cli/textual_ui/app.tcss` — complete Textual CSS stylesheet (1028 lines)
- `vibe/cli/textual_ui/terminal_theme.py` — OSC terminal color probe, `Theme` construction
- `vibe/cli/textual_ui/widgets/welcome.py` — `WelcomeBanner` widget, gradient colors, banner text
- `vibe/cli/textual_ui/app.py` — `VibeApp` (targeted lines 1220-1265 for brand references)
- `vibe/cli/update_notifier/update.py` — `UPDATE_COMMANDS` list (targeted line 125)
- `vibe/cli/update_notifier/adapters/github_update_gateway.py` — User-Agent header (targeted line 34)
- `vibe/acp/acp_agent_loop.py` — `VibeAcpAgentLoop`, ACP `Implementation` metadata, auth methods
- `vibe/acp/entrypoint.py` — ACP CLI bootstrap, argparse description, history greeting
- `vibe/whats_new.md` — release notes

**Folders explored (with `get_source_folder_contents`):**
- Root (`""`) — top-level structure
- `vibe/` — package root
- `vibe/core/` — core runtime modules
- `vibe/core/prompts/` — prompt templates
- `vibe/cli/` — CLI modules
- `vibe/cli/textual_ui/` — Textual UI package
- `vibe/cli/textual_ui/widgets/` — widget library
- `vibe/acp/` — ACP layer
- `tests/` — test suites

**Grep searches conducted:**
- `grep -rn "Mistral Vibe\|mistral-vibe\|Mistral AI\|mistral_vibe\|Mistral.Vibe\|vibe@mistral\|mistralai/mistral-vibe"` across all `*.py`, `*.md`, `*.toml`, `*.yml`, `*.yaml`, `*.spec`, `*.tcss` files
- `grep -rn "VIBE_\|Hello Vibe\|~/.vibe\|\.vibe/"` across source files (excluding internal constants)
- `grep -rn "Mistral Vibe\|mistral-vibe\|Mistral AI\|mistral_vibe\|vibe@mistral\|Hello Vibe"` specifically in `vibe/` and `tests/` Python files
- `grep -rn "Mistral\|mistral\|Vibe\|vibe"` in `vibe/setup/` directory
- `grep -rn "mistral-vibe\|Mistral Vibe\|Mistral AI\|mistral_vibe"` in `.github/`, `distribution/`, `scripts/`, and `docs/` directories
- `find . -name "vibe-acp.spec"` and `find . -name "*.tcss"` for file discovery

**Setup/onboarding files read via grep or bash:**
- `vibe/setup/onboarding/__init__.py` — setup complete message
- `vibe/setup/onboarding/screens/welcome.py` — `WELCOME_HIGHLIGHT` constant, animation logic
- `vibe/setup/onboarding/screens/api_key.py` — API key screen branding (Mistral AI Studio preserved)
- `vibe/setup/trusted_folders/trust_folder_dialog.py` — trust dialog message
- `distribution/zed/extension.toml` — full Zed extension manifest
- `scripts/install.sh`, `scripts/prepare_release.py` — release scripts (via grep)
- `tests/conftest.py` — test configuration fixtures

**Test files searched for brand strings:**
- `tests/acp/test_initialize.py` — ACP initialization assertions
- `tests/acp/test_acp.py` — model name check (preserved)
- `tests/onboarding/test_run_onboarding.py` — onboarding message assertion
- `tests/update_notifier/test_pypi_update_gateway.py` — PyPI project name assertions
- `tests/update_notifier/test_ui_update_notification.py` — update notification assertions

**Tech spec sections retrieved:**
- Section 1.1 EXECUTIVE SUMMARY — project overview and context

### 0.8.2 Attachments

No attachments were provided for this project. No Figma URLs or external design files were referenced.

### 0.8.3 External References

No web searches were required for this rebranding task. The brand mapping is fully specified by the user, and the purple color palette values (`#5B39F3`, `#7C5DF5`, `#4A2DD4`, `#8B7FC7`) are provided in the task specification. The Textual CSS theming system uses standard `$variable` tokens documented in the Textual framework.


