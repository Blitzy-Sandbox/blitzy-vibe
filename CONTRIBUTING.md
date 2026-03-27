# Contributing to Blitzy Agent

Thank you for your interest in Blitzy Agent! We appreciate your enthusiasm and support.

## Current Status

**Blitzy Agent is in active development** — our team is iterating quickly and making lots of changes under the hood. Because of this pace, we may be slower than usual when reviewing PRs and issues.

**We especially encourage**:

- **Bug reports** – Help us uncover and squash issues
- **Feedback & ideas** – Tell us what works, what doesn't, and what could be even better
- **Documentation improvements** – Suggest clarity improvements or highlight missing pieces

## How to Provide Feedback

### Bug Reports

If you encounter a bug, please open an issue with the following information:

1. **Description**: A clear description of the bug
2. **Steps to Reproduce**: Detailed steps to reproduce the issue
3. **Expected Behavior**: What you expected to happen
4. **Actual Behavior**: What actually happened
5. **Environment**:
   - Python version
   - Operating system
   - Blitzy Agent version
6. **Error Messages**: Any error messages or stack traces
7. **Configuration**: Relevant parts of your `config.toml` (redact any sensitive information)

### Feature Requests and Feedback

We'd love to hear your ideas! When submitting feedback or feature requests:

1. **Clear Description**: Explain what you'd like to see or improve
2. **Use Case**: Describe your use case and why this would be valuable
3. **Alternatives**: If applicable, mention any alternatives you've considered

## Development Setup

This section is for developers who want to set up the repository for local development, even though we're not currently accepting contributions.

### Prerequisites

- Python 3.12 or higher
- [uv](https://github.com/astral-sh/uv) - Modern Python package manager

### Setup

1. Clone the repository:

   ```bash
   git clone <repository-url>
   cd blitzy-agent
   ```

2. Install dependencies:

   ```bash
   uv sync --all-extras
   ```

   This will install both runtime and development dependencies.

3. (Optional) Install pre-commit hooks:

   ```bash
   uv run pre-commit install
   ```

   Pre-commit hooks will automatically run checks before each commit.

### Running Tests

Run all tests:

```bash
uv run pytest
```

Run tests with verbose output:

```bash
uv run pytest -v
```

Run a specific test file:

```bash
uv run pytest tests/test_agent_tool_call.py
```

### Linting and Type Checking

#### Ruff (Linting and Formatting)

Check for linting issues (without fixing):

```bash
uv run ruff check .
```

Auto-fix linting issues:

```bash
uv run ruff check --fix .
```

Format code:

```bash
uv run ruff format .
```

Check formatting without modifying files (useful for CI):

```bash
uv run ruff format --check .
```

#### Pyright (Type Checking)

Run type checking:

```bash
uv run pyright
```

#### Pre-commit Hooks

Run all pre-commit hooks manually:

```bash
uv run pre-commit run --all-files
```

The pre-commit hooks include:

- Ruff (linting and formatting)
- Pyright (type checking)
- Typos (spell checking)
- YAML/TOML validation
- Action validator (for GitHub Actions)

### Code Style

- **Line length**: 88 characters (Black-compatible)
- **Type hints**: Required for all functions and methods
- **Docstrings**: Follow Google-style docstrings
- **Formatting**: Use Ruff for both linting and formatting
- **Type checking**: Use Pyright (configured in `pyproject.toml`)

See `pyproject.toml` for detailed configuration of Ruff and Pyright.

## Project Architecture

The `vibe/` package is organized into four top-level modules, each with a distinct responsibility:

| Module | Purpose |
|---|---|
| `vibe/core/` | Orchestration layer — agent loop, tool framework, LLM backend, configuration, session management |
| `vibe/cli/` | User interface layer — Textual-based TUI, CLI commands, autocompletion, update notifications |
| `vibe/acp/` | Editor integration layer — Agent Client Protocol (ACP) for IDE connectivity |
| `vibe/setup/` | Onboarding layer — first-run setup screens, API key configuration, theme selection |

### Architectural Patterns

The codebase follows several key design patterns:

- **Composition over inheritance** — Large classes are decomposed into focused handler classes that are injected via `__init__` constructor parameters. The parent class retains thin one-liner delegation methods that forward calls to the composed handler.
- **Protocol-based type contracts** — `vibe/core/protocols.py` defines shared `typing.Protocol` subclasses (`BackendLike`, `ToolLike`, `ConfigLike`, `ToolManagerLike`) that break circular import chains. Modules reference protocol types under `TYPE_CHECKING` instead of importing concrete implementations directly.
- **Exception specificity** — Production code uses specific exception types (e.g., `OSError`, `httpx.HTTPError`, `asyncio.CancelledError`, `pydantic.ValidationError`) instead of bare `except Exception` blocks.

### Key Modules

| Module | Description |
|---|---|
| `vibe/core/protocols.py` | Shared Protocol classes for import decoupling — contains only `typing.Protocol` subclasses with stdlib-only imports |
| `vibe/core/tool_executor.py` | Tool call handling logic extracted from `AgentLoop` — manages tool invocation, result collection, and missing response backfill |
| `vibe/core/turn_runner.py` | LLM turn orchestration extracted from `AgentLoop` — manages streaming events and single-turn execution |
| `vibe/cli/textual_ui/handlers/command_handler.py` | Slash command dispatch extracted from `VibeApp` — routes `/commands` to their implementations |
| `vibe/cli/textual_ui/handlers/approval_handler.py` | Tool approval flow extracted from `VibeApp` — manages user approval and rejection of tool calls |
| `vibe/cli/textual_ui/handlers/history_handler.py` | Session history rebuild extracted from `VibeApp` — reconstructs conversation history from persisted sessions |

### Coding Standards

All Python code in the `vibe/` package must follow the conventions documented in [`AGENTS.md`](AGENTS.md). Key rules include:

- **Explicit public API** — Every `.py` file must export its public API via `__all__`.
- **PEP 563 annotations** — `from __future__ import annotations` is required in all `vibe/` source files.
- **No suppression annotations** — Do not introduce new `# noqa`, `# type: ignore`, or `# pragma: no cover` comments. Fix the underlying issue instead.
- **No bare exception handlers** — Use specific exception types rather than `except Exception`. Choose the narrowest applicable type based on the operations performed.
- **Ruff** — Line length 88, Python 3.12 target, preview mode enabled. See `[tool.ruff]` in `pyproject.toml` for the full lint rule set.
- **Pyright** — Strict mode enabled for all `vibe/**/*.py` and `tests/**/*.py` files with Python 3.12 as the target version.
- **Pylint limits** — `max-statements=50`, `max-branches=15`, `max-locals=15`, `max-args=9`, `max-returns=6`, `max-nested-blocks=4`.

### Suggested Next Tasks

The following improvement areas were identified during the structural refactoring:

- **Further god class decomposition** — Additional large classes in `vibe/cli/textual_ui/widgets/` may benefit from further extraction as the UI layer grows.
- **Expanded Protocol coverage** — Extend the `vibe/core/protocols.py` pattern to decouple additional cross-module type dependencies beyond the current four protocol classes.
- **Test coverage for extracted modules** — The new `tool_executor.py`, `turn_runner.py`, and handler modules (`command_handler.py`, `approval_handler.py`, `history_handler.py`) should have dedicated unit tests validating their individual contracts.
- **Cognitive complexity reduction** — Continue reducing C901 complexity scores in functions flagged by `uv run ruff check --select C901 vibe/`, targeting scores below the current baseline.

## Code Contributions

While we're not accepting code contributions at the moment, we may open up contributions in the future. When that happens, we'll update this document with:

- Pull request process
- Contribution guidelines
- Review process

## Questions?

If you have questions about using Blitzy Agent, please check the [README](README.md) first. For other inquiries, feel free to open a discussion or issue.

Thank you for helping make Blitzy Agent better! 🙏
