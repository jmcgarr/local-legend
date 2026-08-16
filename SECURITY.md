# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: the **Security** tab of
this repository → **Report a vulnerability**. Do not open a public issue for
security problems.

## Scope

This is a local CLI tool with no server component. The security-relevant
surface is small but real:

- **Credential storage** — the Strava Client Secret and OAuth tokens are
  stored in the operating system keychain (via `keyring`), never on disk.
- **OAuth callback** — a temporary localhost HTTP server receives the
  one-time authorization code during login.
- **Cache** — `~/.cache/strava-legends/` contains your activity and segment
  data (no credentials), readable only by your user account.

Reports about leaking credentials or personal data through any of these
paths are very welcome.

## Supported versions

Only the latest release is supported. There is no backporting.
