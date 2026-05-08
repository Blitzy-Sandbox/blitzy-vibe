# Migration Checks (Alembic)

Only run if changed files include Alembic migration files (`alembic/versions/*.py` or similar).

## 1. Detect Migration Files

Look for changed files matching:
- `*/alembic/versions/*.py`
- `*/migrations/versions/*.py`

If none found, SKIP all migration checks.

## 2. Find alembic.ini

From the migration file path, walk up to find `alembic.ini` in the subproject root.

## 3. Upgrade Round-Trip

```bash
cd <subproject root with alembic.ini>

# Check current head
alembic heads

# Attempt upgrade
alembic upgrade head

# Attempt downgrade back
alembic downgrade -1

# Restore to head
alembic upgrade head
```

- Severity: **BLOCKING** if upgrade or downgrade fails
- Requires a database connection — if DB is not available, record as SKIP with note "no DB connection available for migration test"

## 4. Migration Consistency

```bash
# Check for multiple heads (branching)
alembic heads
```

- If more than one head exists, severity: **WARNING** — "multiple migration heads detected, may need merge"
