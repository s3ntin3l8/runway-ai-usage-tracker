# Security Guidelines

## Credential Management

### Local Development

1. **Environment Variables (.env)**
   - All credentials are stored in `.env` (NOT committed to git)
   - `.env` is listed in `.gitignore`
   - Use `.env.example` as a template for new developers

2. **Required Credentials**
   ```bash
   OPENCODE_GO_API_KEY=sk-...
   GITHUB_TOKEN=github_pat_...
   ZAI_API_KEY=sk-zai-...
   KIMI_API_KEY=sx...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-...
   GEMINI_OAUTH_CLIENT_ID=...
   GEMINI_OAUTH_CLIENT_SECRET=...
   INGEST_API_KEY=...  (Required secret shared between server and sidecars)
   ```

## Ingestion API Security (Sidecar)

Runway uses a cryptographically secure ingestion mechanism for sidecars (Mode 2 and Mode 3).

### HMAC-SHA256 Signing

To prevent token theft and replay attacks, all requests to `POST /api/ingest` must be signed using HMAC-SHA256:

1. **Shared Secret**: The `INGEST_API_KEY` (from `.env`) acts as the HMAC secret.
2. **Payload**: The signature is calculated over the concatenation of:
   - `X-Timestamp`: Current Unix timestamp
   - The raw JSON request body
3. **Verification**: The server re-calculates the signature and verifies that:
   - The signature matches (`hmac.compare_digest`).
   - The timestamp is within a 5-minute sliding window of the server's current time.

### Security Implications

- **No Cleartext Keys**: The `INGEST_API_KEY` is never sent over the network (unlike the previous static key model).
- **Replay Protection**: An attacker cannot capture and reuse a valid request after 5 minutes.
- **Payload Integrity**: Any modification to the metrics or tokens in transit will invalidate the signature.

> [!IMPORTANT]
> Always use HTTPS (SSL/TLS) for the `APP_HOST` when deploying in non-local environments (Docker/Server) to provide an additional layer of encryption for the body content.

### Pre-commit Hooks

This project uses pre-commit hooks to prevent accidental credential commits.

**Installation:**
```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files  # Test on existing files
```

**What it checks:**
- `detect-secrets` - Scans for API keys, tokens, and secrets using entropy analysis
- `trailing-whitespace` - Removes trailing whitespace
- `end-of-file-fixer` - Fixes file endings
- `check-added-large-files` - Prevents large binary files
- `check-json` - Validates JSON syntax
- `black` - Python code formatting (optional)

**Manual check:**
```bash
# Check all files
pre-commit run --all-files

# Check only staged files (automatic on commit)
pre-commit run
```

### Git Configuration

1. **Before pushing:**
   ```bash
   git status  # Verify .env is NOT staged
   git log -p -- .env | head -20  # Verify no credentials in history
   ```

2. **Never commit .env:**
   ```bash
   # ❌ Bad
   git add .env
   git commit -m "Add credentials"

   # ✅ Good
   git add .env.example  # Template only
   ```

### CI/CD Pipeline

For GitHub Actions or other CI/CD:

1. **Use GitHub Secrets** instead of .env files
2. **Inject at runtime:**
   ```yaml
   - name: Run tests
     env:
       GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
       OPENCODE_GO_API_KEY: ${{ secrets.OPENCODE_GO_API_KEY }}
     run: python scripts/test_*.py
   ```

3. **Never log secrets:**
   ```python
   # ❌ Bad
   print(f"Using token: {token}")
   logger.info(f"API Key: {api_key}")

   # ✅ Good
   logger.info("Authenticating with API key")
   logger.debug(f"Token length: {len(token)} chars")
   ```

## Credential Rotation

### When to Rotate

- After exposure in git history (even if filtered)
- Regularly (quarterly or semi-annually)
- If team member leaves
- After security incident

### How to Rotate

1. **Generate new credentials** from each service:
   - OpenCode: https://opencode.ai/settings
   - GitHub: https://github.com/settings/tokens
   - Gemini: https://console.developers.google.com
   - etc.

2. **Update .env locally:**
   ```bash
   cp .env .env.backup  # Backup first
   # Edit .env with new credentials
   ```

3. **Test new credentials:**
    ```bash
    python -m pytest tests/unit/test_collectors.py -v
    python scripts/sidecar.py --provider anthropic --dry-run
    python scripts/sidecar.py --provider gemini --dry-run
    ```

4. **Revoke old credentials** from each service

5. **No git commit needed** - .env is not tracked

### Revoking Compromised Credentials

If credentials were exposed (as with the Gemini OAuth issue):

**Gemini OAuth:**
1. Go to https://console.developers.google.com
2. Select your project
3. Delete the OAuth 2.0 Client ID
4. Create a new one
5. Update GEMINI_OAUTH_CLIENT_ID and GEMINI_OAUTH_CLIENT_SECRET in .env

**GitHub Token:**
1. Go to https://github.com/settings/tokens
2. Find the token and click "Delete"
3. Create a new token with same scopes
4. Update GITHUB_TOKEN in .env

## Audit Trail

### Previous Exposure

**Issue**: Gemini OAuth credentials hardcoded in source (RESOLVED)
- **Commit**: d1738aa (before filter-branch)
- **Files**: app/services/collectors/gemini.py, scripts/sidecar.py, scripts/test_gemini_api.py
- **Status**: Removed via git filter-branch, credentials rotated
- **Date**: 2026-04-07

### Future Prevention

- Pre-commit hooks enabled
- `.secrets.baseline` configured
- This documentation in place
- Regular credential rotation scheduled

## Questions?

Refer to `.env.example` for required keys or consult the README for setup instructions.
