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

## Special-processing jobs

`special_workflow` is the long-lived interactive state; `special_job` is one
Supervisor-launched process request. Manga `remark` contains only a generated
display summary and is never parsed to schedule a job. An archive in
`special_processing` is excluded from ordinary task claims.

For a `video_torrent` review item, the ordinary archive page offers the generic
enabled extension entry “进入视频种子下载与整合”. Select one image and one video candidate. Submission ends after
qBittorrent reports both hashes; no worker remains resident and no automatic
download polling is scheduled. Later use the Web “特殊处理” page’s batch check,
or run:

```text
eharchive --config-dir config special video-archive collect-ready
```

The dispatcher only creates one `check_and_compose_if_ready` job per eligible
workflow. An incomplete pair returns normally to `downloading` and must be
checked again manually. A complete pair proceeds in the same one-shot job
through safe extraction, MP4-to-WebP conversion, deterministic packing and
artifact registration, then returns the Manga to `downloaded` for the ordinary
validator.

The completed workflow retains both special Torrents and its workspace while
the Manga passes through the unchanged ordinary validate, upload and cleanup
pipeline. After the Manga reaches `completed`, use the Web source-cleanup
button or run `eharchive --config-dir config special video-archive
cleanup-completed`. This manual dispatcher creates one
`cleanup_sources_after_complete` job per eligible workflow. Supervisor never
creates special jobs from status changes. The cleanup worker verifies the
stored hashes, exact category and numeric-ID save paths before deleting the two
qBittorrent tasks/files and `workspace_root/<numeric-id>/w<workflow-id>`.

While a job is queued or running, the HTMX workflow panel reads persisted
PostgreSQL workflow/job state and declarative module enablement only. It never
probes the filesystem, starts ffmpeg, or polls qBittorrent. The manual compose
job checks ffmpeg, `libwebp`, local content and the workspace only after both
Torrents report complete. The final ZIP uses fixed entry
metadata and stable member ordering, so a retry after “file promoted but DB
commit failed” can compare the rebuilt SHA-1 with the existing generation.

An expired special-job lease is never reclaimed automatically. Confirm the old
process has stopped, then use the workflow page’s expired-lease action. If the
module configuration is missing or disabled, the page offers a controlled exit
that restores the Manga state while explicitly retaining qBittorrent tasks and
work files. The same retain-and-exit choice is available when cleanup is not
wanted. A running worker receives a database cancellation request and responds
at a safe point; Web never kills it directly. “取消并清理” is only available
while the module configuration is healthy and deletes only hashes that still
match the exact special category and deterministic save path.
Queued jobs can also be cancelled before Supervisor claims them. Cancelling or
retaining resources on exit restores the original `video_torrent` review reason,
so the archive detail page can offer the module again later.
Any extension-worker exit is confined to its special job and retry cooldown; it
does not place the ordinary Supervisor pipeline into draining.

Forced deletion is queued through the archive detail page only. The Web action
can move `uploaded`, `completed`, `outdated`, or `manual_review` to
`force_delete_pending` after a required reason and manga-ID confirmation. No
automatic transition enters this state. The normal `delete` worker then skips
the replacement-readiness check but keeps the same LANraragi deletion, local
artifact removal, attempt fencing, error handling, and final `deleted` state as
an ordinary `outdated` deletion.

For an ordinary `outdated` archive, the Web only requires an existing,
different replacement manga ID. The delete worker waits until that replacement
has reached `download_pending` or a later normal pipeline state. A replacement
that is still merely discovered, or has entered an exceptional terminal state,
leaves the old archive in `outdated`; it does not cause automatic deletion.
