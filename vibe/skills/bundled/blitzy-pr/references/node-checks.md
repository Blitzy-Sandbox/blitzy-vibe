# Node/TypeScript Static Checks

Run these checks scoped to subprojects with changed `.ts`/`.tsx`/`.js`/`.jsx` files.

## 1. TypeScript Compile Check

```bash
cd <subproject root>
yarn tsc --noEmit 2>&1
# or: npx tsc --noEmit
```

- Severity: **BLOCKING**
- Only run if `tsconfig.json` exists in the subproject root.
- If `node_modules` doesn't exist, run `yarn install --frozen-lockfile` first.

## 2. Test Execution

```bash
cd <subproject root>
yarn test 2>&1
# or: npm test
```

- Severity: **BLOCKING**
- Validate on exit code.
- If no test script in `package.json`, record as SKIP.

## 3. Lint Check

```bash
cd <subproject root>
yarn lint 2>&1
# or: npx eslint <changed files>
```

- Severity: **WARNING**
- Only run if a lint script exists in `package.json` or `.eslintrc*` exists.
- If not available, record as SKIP.

## Detecting package manager

Check subproject root for:
1. `yarn.lock` → use `yarn`
2. `pnpm-lock.yaml` → use `pnpm`
3. `package-lock.json` → use `npm`
