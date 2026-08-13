# Security Policy

## Supported version

Security fixes target the latest commit on `main`.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could expose local files, credentials, photo metadata, or the loopback API. Use GitHub's **Security → Report a vulnerability** flow for this repository. Include reproduction steps, affected platform/version, and the smallest safe proof of concept.

## Local security model

MemoLens is local-first, not permission-free. The desktop shell and Flask API handle sensitive paths, photo contents, video frames, audio, timelines, and rendered artifacts, so they must:

- bind to loopback by default;
- verify that a service on the configured port is actually MemoLens;
- validate library-relative paths before serving files;
- reject remote callers for settings, indexing, and file endpoints;
- require an authenticated, scoped capability for costly or state-changing video jobs;
- compile typed timeline operations into fixed FFmpeg argument arrays, never model-authored shell;
- write previews to an app-managed root and publish desktop Save As artifacts through bounded temporary files without overwriting an existing destination;
- keep tokens and model credentials out of logs, screenshots, exports, and source control.

Do not expose port `5519` through a public tunnel or bind it to a non-loopback interface unless you add authentication and understand the consequences.

Originless loopback requests remain available for trusted same-user tools such as
Photon and `curl`; this treats other processes running as your local user as part
of the trust boundary. Browser and desktop calls use stricter origin/session
checks. The Codex plugin keeps loopback API mode disabled unless you explicitly
opt in; its default integration opens the SQLite index read-only.

Creator Memory and Media Inbox decisions live as immutable revisions in the
active media database. New material has no review row and is treated as Inbox;
Keep, Archive, Favorite, Ready, Reset, and Undo only change MemoLens metadata.
They never move, overwrite, or delete a source file. Profile suggestions are
read-only observations until a user confirms them in the desktop App.

That explicit read-risk opt-in is not a write credential. It never authorizes
Codex to edit a creator profile, apply Inbox decisions, save timelines, start
FFmpeg, export files, or cancel jobs. In this release, App writes require the
per-launch authenticated desktop session plus active-database binding and
idempotency. The plugin exposes no write tool. Any future Codex-initiated write
would require a separate short-lived, scope-limited capability issued through a
visible desktop confirmation flow.

Video analysis is offline even when a cloud model key already exists.
No external-video opt-in or final-export grant flow is exposed in this release;
both capabilities fail closed. Original media is never overwritten. Saving a
completed preview requires the native desktop dialog and fails if the chosen
destination already exists.
