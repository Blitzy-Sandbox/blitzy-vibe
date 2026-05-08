---
name: test
description: Generate and run tests for specified files, auto-detecting the project's test framework and conventions
allowed-tools: [Read, Write, Edit, Glob, Grep, "Bash(git diff:*)", "Bash(git status:*)", "Bash(pytest:*)", "Bash(python3 -m pytest:*)", "Bash(npx jest:*)", "Bash(npx vitest:*)", "Bash(go test:*)", "Bash(cargo test:*)", "Bash(npx mocha:*)", "Bash(bundle exec:*)", "Bash(node:*)"]
---

# Test Generator — Framework-Aware Test Scaffolding

You are generating and optionally running tests for source files in the current project, matching the project's existing test framework and conventions.

## Arguments

`$ARGUMENTS` may contain any combination of:

| Flag | Effect |
|------|--------|
| `path/to/file.ext` | Target a specific file for test generation |
| `--framework <name>` | Override detected framework (pytest, jest, vitest, go, cargo, mocha, rspec, phpunit) |
| `--update` | Add tests to an existing test file instead of creating a new one |
| `--run` | Run tests after writing to verify they pass |
| `--coverage` | Focus on untested public functions, skip already-tested code |
| *(plain text)* | Specific behavior or scenarios to test |

## Step 1: Identify Target Files

Determine which files to generate tests for:

1. **Explicit path in `$ARGUMENTS`** — use the specified file(s).
2. **No path specified** — auto-detect from recent changes:
   - Run `git diff --name-only HEAD` for unstaged changes
   - Run `git diff --name-only --cached` for staged changes
   - Filter to source files only (exclude tests, configs, lockfiles, docs)
3. If no changes are found and no path was given, tell the user to specify a file and stop.

Read each target file in full using the Read tool.

## Step 2: Detect Test Framework

Check project configuration files to determine the test framework:

| Config File | Framework |
|-------------|-----------|
| `pyproject.toml` with `[tool.pytest]` or `pytest.ini` or `conftest.py` | pytest |
| `package.json` with `jest` in devDependencies or `jest.config.*` | Jest |
| `package.json` with `vitest` in devDependencies or `vitest.config.*` | Vitest |
| `go.mod` | Go testing |
| `Cargo.toml` | Rust `#[cfg(test)]` |
| `package.json` with `mocha` in devDependencies or `.mocharc.*` | Mocha |
| `Gemfile` with `rspec` or `.rspec` | RSpec |
| `composer.json` with `phpunit` or `phpunit.xml` | PHPUnit |

If `--framework` was specified, use that instead of auto-detection.

If no framework is detected, infer from the target file's language:
- Python -> pytest
- JavaScript/TypeScript -> Jest
- Go -> Go testing
- Rust -> Rust built-in tests

## Step 3: Learn Project Test Conventions

Read 2-3 existing test files to extract patterns:

1. **Find existing tests** — search for files matching common test patterns:
   - `test_*.py`, `*_test.py`, `tests/`, `__tests__/`
   - `*.test.ts`, `*.spec.ts`, `*.test.js`, `*.spec.js`
   - `*_test.go`
   - `tests/` directory in Rust crates
2. **Extract patterns** from existing tests:
   - **Naming**: `test_<function>`, `describe/it` blocks, `Test<Struct>` functions
   - **File location**: colocated (`src/foo.test.ts`) vs separate directory (`tests/test_foo.py`)
   - **Imports**: how the project imports test utilities, fixtures, mocks
   - **Assertion style**: `assert`, `expect()`, `assertEqual`, custom matchers
   - **Setup/teardown**: fixtures, `beforeEach`, `setUp`, test helpers
   - **Mocking**: how external dependencies are mocked (mock libraries, dependency injection, test doubles)
3. If no existing tests are found, use the framework's standard conventions.

## Step 4: Determine Test File Path

Follow the project's existing convention for test file placement:

| Convention | Example |
|------------|---------|
| Colocated | `src/utils.ts` -> `src/utils.test.ts` |
| Mirror directory | `src/utils.py` -> `tests/test_utils.py` |
| Same package (Go) | `pkg/handler.go` -> `pkg/handler_test.go` |
| Inline (Rust) | Tests go in `#[cfg(test)] mod tests` at bottom of source file |

If `--update` was specified, find and use the existing test file for this source file.

## Step 5: Analyze Target Code

For each target file, identify what to test:

1. **Public API** — exported functions, classes, methods, types
2. **Branching logic** — if/else, match/switch, early returns, guard clauses
3. **Error paths** — try/catch, Result/Option handling, error returns, validation failures
4. **Edge cases** — empty inputs, boundary values, nil/null/undefined, zero, negative numbers, empty collections
5. **Side effects** — database calls, network requests, file I/O, environment variable reads (these need mocking)

If `--coverage` was specified:
1. Read the existing test file for this source
2. Identify which public functions already have tests
3. Only generate tests for untested functions

## Step 6: Generate Tests

Write tests following these principles:

### Structure
- **One test per behavior** — each test verifies a single logical behavior, not a single function
- **Arrange-Act-Assert** — clear separation of setup, execution, and verification
- **Descriptive names** — test names describe the scenario and expected outcome, e.g., `test_parse_returns_none_for_empty_input`

### What to Test
- Happy path with typical inputs
- Edge cases identified in Step 5
- Error paths and validation failures
- Boundary conditions (empty, single element, max values)
- Return values AND side effects where relevant

### What NOT to Test
- Private/internal functions directly (test through public API)
- Third-party library behavior
- Trivial getters/setters with no logic
- Implementation details that could change without affecting behavior

### Mocking
- Mock external dependencies only (network, database, filesystem, time)
- Do NOT mock the code under test
- Use the project's existing mock patterns (from Step 3)
- Prefer dependency injection over monkey-patching when possible

### Test Quality
- Tests should fail for the right reasons — a test that never fails is useless
- Avoid brittle assertions on exact strings, timestamps, or ordering unless order is part of the contract
- Use factory functions or fixtures for complex test data instead of inline object literals

## Step 7: Write Test File

### Creating a new test file:
Use the Write tool. Include all necessary imports, fixtures, and the generated tests.

### Updating an existing test file (`--update`):
Use the Edit tool. Add new test functions after the existing ones, maintaining the file's import style and organization.

### After writing, show a summary:

```
## Tests Generated

| Source File | Test File | Tests Added |
|-------------|-----------|-------------|
| `path/to/source.py` | `tests/test_source.py` | 8 |

### Test Inventory
- `test_function_a_returns_expected_output` — happy path
- `test_function_a_raises_on_empty_input` — edge case
- `test_function_b_handles_network_error` — error path
...
```

## Step 8: Run Tests (if `--run`)

If `--run` was specified in `$ARGUMENTS`:

1. **Execute tests** using the detected framework:
   - pytest: `pytest path/to/test_file.py -v`
   - Jest: `npx jest path/to/test_file --verbose`
   - Vitest: `npx vitest run path/to/test_file --reporter=verbose`
   - Go: `go test ./path/to/package/ -v -run TestName`
   - Cargo: `cargo test test_module_name -- --nocapture`
   - Mocha: `npx mocha path/to/test_file`
   - RSpec: `bundle exec rspec path/to/spec_file`

2. **If tests fail**, analyze the failure:
   - **Test-side bug** (wrong assertion, missing mock, import error) — fix the test and retry. Up to 3 retries.
   - **Source-side bug** (the test correctly caught a real bug) — report it to the user, do NOT modify the source code. Format as:
     ```
     ## Source Bug Detected

     **File:** `path/to/source.py:42`
     **Test:** `test_function_a_raises_on_empty_input`
     **Issue:** [Description of the bug the test revealed]
     ```

3. **After tests pass** (or after 3 failed retries), show results:
   ```
   ## Test Results

   Passed: N
   Failed: N
   Skipped: N
   Total:  N
   ```

## Composability

After generating tests, suggest next steps:

- "Run `/commit` to commit the new tests."
- If source bugs were found: "Consider running `/review` on the source file to investigate further."
- If `--run` wasn't used: "Add `--run` to execute the tests and verify they pass."

## Edge Cases

- **No test framework detected and no `--framework`**: Default based on language. If the language is ambiguous, ask the user which framework to use.
- **Target file is already a test file**: Tell the user and stop. Don't generate tests for tests.
- **Target file doesn't exist**: Tell the user the file wasn't found and stop.
- **Generated file has no public API**: Warn the user there's nothing to test publicly. Offer to test internal functions if the user confirms.
- **Rust inline tests**: When the convention is inline `#[cfg(test)]` modules, use Edit to append the test module to the source file instead of creating a separate file.
- **Monorepo**: If the project has multiple packages (workspace), detect and use the correct package's test config.
