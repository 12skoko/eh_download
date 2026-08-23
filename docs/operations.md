# Operations

`GET /health` reports PostgreSQL reachability, Supervisor heartbeat, component
pause state and status counts. A component can be paused with
`PUT /api/control/{component}`; setting `supervisor` to `paused` prevents new
task claims. Setting it to `draining` waits for in-flight work to finish and
then stops the Supervisor cleanly.

The Supervisor periodically checks qBittorrent, LANraragi and every configured
storage root, then persists the latest result in `system_health`. The Web
process only reads these snapshots. A snapshot older than three check intervals
is displayed as stale.

Every task attempt has a lease token and artifact generation. A process that
loses its lease cannot update the manga row or replace the current artifact.
Supervisor does not reclaim expired leases automatically. After a forced
process termination, inspect the affected attempt and external
qBittorrent/LANraragi state before using the archive detail page's expired
lease release action. The action abandons the running attempt, moves the archive
to `manual_review`, clears its lease fields and records an audit event. It does
not stop an operating-system process, run cleanup or delete artifacts.

Thumbnail regeneration is a separate, idempotent batch. It records its result
in `event_log` and never changes an archive from `uploaded` or `completed`.

Use the Web action endpoints for retry, cancellation, review recovery and
manual archive confirmation. Never put cookies, authorization values or proxy
credentials in event details; secrets are loaded from `secrets.toml` or
environment variables and are never returned by the API.

Forced deletion is queued through the archive detail page only. The Web action
can move `uploaded`, `completed`, `outdated`, or `manual_review` to
`force_delete_pending` after a required reason and manga-ID confirmation. No
automatic transition enters this state. The normal `delete` worker then skips
the replacement-readiness check but keeps the same LANraragi deletion, local
artifact removal, attempt fencing, error handling, and final `deleted` state as
an ordinary `outdated` deletion.
