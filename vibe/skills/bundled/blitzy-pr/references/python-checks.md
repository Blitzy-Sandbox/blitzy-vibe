# Python Static Checks

Run these checks scoped to changed `.py` files only.

## 1. Format Check (black)

```bash
black --check --diff <changed .py files>
```

- Severity: **BLOCKING**
- If `black` is not installed in the subproject's venv, record as SKIP with note.

## 2. Import Sort Check (isort)

```bash
isort --check-only --diff <changed .py files>
```

- Severity: **BLOCKING**
- If `isort` is not installed, record as SKIP.

## 3. Compile Check (py_compile)

```bash
python3 -m py_compile <each changed .py file>
```

- Severity: **BLOCKING**
- Catches syntax errors. Run on every changed `.py` file individually. Report which files fail.

## 4. Test Execution (pytest)

```bash
# Detect test directory from changed files' subproject
# Run only tests relevant to the subproject
pytest <subproject test dir> --tb=short -q
```

- Severity: **BLOCKING**
- Validate on exit code, not test count.
- If no test directory found, record as SKIP.
- If pytest is not installed, record as SKIP.

## Finding the right virtualenv

Look for these in the subproject root, in order:
1. `.venv/bin/python`
2. `venv/bin/python`
3. Fall back to system `python3`

Activate with `source .venv/bin/activate` before running checks.
