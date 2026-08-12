# Security Policy

## Supported version

Security fixes target the latest commit on `main`.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose local files, credentials, photo metadata, or the loopback API. Use GitHub's **Security → Report a vulnerability** flow for this repository. Include reproduction steps, affected platform/version, and the smallest safe proof of concept.

## Local security model

MemoLens is local-first, not permission-free. The desktop shell and Flask API handle sensitive paths and photo contents, so they must:

- bind to loopback by default;
- verify that a service on the configured port is actually MemoLens;
- validate library-relative paths before serving files;
- reject remote callers for settings, indexing, and file endpoints;
- keep tokens and model credentials out of logs, screenshots, exports, and source control.

Do not expose port `5519` through a public tunnel or bind it to a non-loopback interface unless you add authentication and understand the consequences.

Originless loopback requests remain available for trusted same-user tools such as
Photon and `curl`; this treats other processes running as your local user as part
of the trust boundary. Browser and desktop calls use stricter origin/session
checks. The Codex plugin keeps loopback API mode disabled unless you explicitly
opt in; its default integration opens the SQLite index read-only.
