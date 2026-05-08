# Security Checks

Run against the full diff of the PR.

## 1. Secrets in Diff

```bash
# Get the full diff
gh pr diff 2>/dev/null || git diff main...HEAD

# Scan for patterns
```

Search the diff output for these patterns (added lines only, lines starting with `+`):

- API keys: `(api[_-]?key|apikey)\s*[:=]\s*['"][A-Za-z0-9]`
- AWS keys: `AKIA[0-9A-Z]{16}`
- Generic secrets: `(secret|password|token|credential)\s*[:=]\s*['"][^'"]{8,}`
- Private keys: `-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----`
- Connection strings: `(postgres|mysql|mongodb|redis)://[^\s]+@`

- Severity: **BLOCKING** if any match found

## 2. Sensitive Files

Check if any of these files appear in the changed file list:

- `.env`, `.env.*` (except `.env.example`, `.env.template`)
- `credentials.json`, `service-account*.json`
- `*.pem`, `*.key` (private keys)
- `*secret*` files (unless in documentation)

- Severity: **BLOCKING** if found

## 3. Hardcoded URLs

Flag non-localhost URLs that look like internal services:
- `http://` (non-HTTPS) to non-localhost targets
- Internal domain patterns

- Severity: **WARNING**
