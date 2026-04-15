# Security Guidelines

## 🛡️ Core Principles

1. **Local-First & Database-Backed**: Runway uses an encrypted SQLite database to store credentials, configuration, and usage history. The `DB_ENCRYPTION_KEY` (from `.env`) encrypts sensitive fields at rest. Credentials and tokens can also be supplied via environment variables for stateless deployments.
2. **Server-Side API Calls**: All external provider API calls are made from the main application server, never the sidecar.
3. **Signed Ingestion**: Data sent from sidecars to the server is cryptographically signed to ensure integrity and authenticity.

## 🔑 Credential Management

### Environment Variables (.env)
Credentials MUST be stored in a `.env` file at the project root. This file is excluded from version control via `.gitignore`. 
Refer to [.env.example](file:///.env.example) for the required structure.

### Automatic Protection (Pre-commit)
The project uses `pre-commit` hooks to prevent accidental credential leaking.

**Installation:**
```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files  # Optional: Test on existing files
```

**What it checks:**
- `detect-secrets` - Scans for API keys, tokens, and secrets
- `check-json` - Validates JSON syntax
- `trailing-whitespace` & `end-of-file-fixer`

## 🛰️ Ingestion API Security (Sidecars)

In Multi-Host or Docker modes, sidecars send metrics and tokens via `POST /api/ingest`. To prevent replay attacks and token theft, Runway uses **HMAC-SHA256 Signing**:

1. **Shared Secret**: The `INGEST_API_KEY` (from `.env`) is the HMAC secret.
2. **Signature Generation**: 
   - Sidecars calculate a signature over the JSON body and a `X-Timestamp`.
   - The server verifies the signature matches and that the timestamp is within a 5-minute sliding window.
3. **Requirement**: Always use **HTTPS** for the `APP_HOST` in production to encrypt the request body during transit.

## 🔄 Maintenance & Hygiene

### Credential Rotation
Rotate credentials quarterly or immediately if you suspect exposure.
1. Generate a new key/token from the provider dashboard (GitHub, Anthropic, etc.).
2. Update the value in your local `.env`.
3. Revoke the old key/token immediately.

### CI/CD Security
When using GitHub Actions or other CI/CD pipelines:
- Use **Encrypted Secrets** (e.g., GitHub Repository Secrets).
- Never log secrets to standard output or log files.
- Inject secrets into the environment at runtime using the `env:` block in YAML.

## 👮 Reporting Vulnerabilities
If you discover a security vulnerability, please open a private security advisory on GitHub or contact the maintainer directly.
