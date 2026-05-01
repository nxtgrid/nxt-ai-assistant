# Security Policy

## Supported Versions

We provide security fixes for the latest version on the `main` branch.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report security issues privately via [GitHub Security Advisories](../../security/advisories/new) or by emailing the maintainers (see AUTHORS.md).

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- Any suggested mitigations

We will acknowledge receipt within 5 business days and aim to resolve confirmed vulnerabilities within 30 days.

## Scope

- Credential exposure in the codebase
- Authentication/authorization bypass in the chat API
- Injection vulnerabilities (prompt injection, SQL injection, command injection)
- Insecure direct object references in multi-tenant data

## Out of Scope

- Vulnerabilities in third-party dependencies (report upstream)
- Issues that require physical access to a deployment
- Social engineering attacks

## Deployment Notes

Anansi requires several external credentials (Google API key, Supabase, Telegram bot token, etc.). Operators are responsible for:
- Rotating credentials regularly
- Using environment variables — never committing secrets
- Restricting `EQUIPMENT_CONTROL_ALLOWED_USERS` and `ALLOWED_VIEWER_EMAILS` to trusted staff
- Keeping dependencies updated (`pip install --upgrade`)
