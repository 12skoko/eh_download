# Operations

`GET /health` reports PostgreSQL reachability, Supervisor heartbeat, component
pause state and status counts. A component can be paused with
`PUT /api/control/{component}`; setting `all` to `paused` prevents new task
claims while in-flight work finishes at its next safe boundary.

Every task attempt has a lease token and artifact generation. A process that
loses its lease cannot update the manga row or replace the current artifact.
Supervisor does not reclaim expired leases automatically. After a forced
process termination, inspect the affected attempt and external
qBittorrent/LANraragi state before clearing or replacing its lease manually.

Thumbnail regeneration is a separate, idempotent batch. It records its result
in `event_log` and never changes an archive from `uploaded` or `completed`.

Use the Web action endpoints for retry, cancellation, review recovery and
manual archive confirmation. Never put cookies, authorization values or proxy
credentials in event details; secrets are loaded from `secrets.toml` or
environment variables and are never returned by the API.
