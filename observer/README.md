# Local read-only Claude observation reports

`report.py` uses Python 3.10+ and the standard library. This component reads native
samples and explicit local event sources, then writes local derived files. It
does not launch or control Claude, submit prompts, call models, read credentials,
upload data, or parse desktop chat databases. Native sampling, helper scheduling,
the stop control and deadline enforcement belong to the collector.

The reporter uses the native start record's finite deadline; elapsed wall time
alone never establishes completion. It does not install an autostart task.

Run from the repository root, supplying an existing private output directory
outside Git. Paths below are placeholders; nothing is automatically discovered:

```powershell
python -B observer/report.py --collect-events --data "D:\private-observation" --history "D:\explicit\history.jsonl" --projects "D:\explicit\projects" --since-start
python -B observer/report.py --data "D:\private-observation" --since-start --refresh
python -B observer/report.py --data "D:\private-observation" --since-start
```

The metadata helper runs one bounded pass and exits. The native collector can
invoke it every 60 seconds in its separately limited helper job. It creates a
preview on the first pass and refreshes it at most once every five minutes.
Final reporting consumes only the previously collected metadata feed. Collection
refuses source access at or after the native deadline. Run a single
writer per output directory. Reports may refresh while native samples are open;
an incomplete last record is deferred. A valid native `end` determines status:
`deadline` is completed, `requested` is stopped, and `limit`/`error` are stopped
with those reasons. `helper` diagnostics before or after `end` never establish
Claude completion. Final reporting accepts source arguments for compatibility
but does not open the original logs. Native `completion.json` separately records
whether the final report helper actually succeeded; a sampling end alone does
not establish reporting success. The final report is written before that helper
exits, so its own successful exit is confirmed by this separate completion file.

## Inputs and bounds

`samples.jsonl` is native-owned, newline-terminated JSONL, at most 64 MiB, with
128 KiB records and at most 100,000 accepted samples. The first record is:

```json
{"type":"start","version":1,"interval_ms":5000,"started_unix_ms":1700000000000,"deadline_unix_ms":1700003600000,"aggregation":"root_groups","cpu_scope":"observed_member_deltas"}
```

Use the actual native timestamps; the example only illustrates the schema.
Each sample has `type`, `ts_ms`, `host_total_bytes`, `host_available_bytes`,
`collector_private_bytes`, `collector_working_set_bytes`, `collector_cpu_100ns`,
`processes`, `claude_foreground` and `snapshot_incomplete`. Each process row is a
**root-group aggregate**, with `pid == root_pid`, `created_100ns ==
root_created_100ns`, `ppid`, `name: "claude.exe"`, `private_bytes`,
`working_set_bytes`, `cpu_100ns`, and `member_count`. There are at most 16 groups
and 4,096 total reported members per sample. Native `cpu_100ns` is an accrued
lower bound of continuously observed member deltas in that root group. A valid
end is `{"type":"end","ts_ms":1700003600000,"reason":"deadline"}` with reason
`deadline`, `requested`, `limit`, or `error`. Helper diagnostics use
`{"type":"helper","ts_ms":1700003600000,"final":true,"exit_code":0}`.

The selective metadata parser walks bounded byte records without decoding or
copying unknown JSON string values into Python strings. Only bounded metadata
keys, timestamp, record type, role and content-block types are decoded. Prompt
`display`, `pastedContents`, `text`, `lastPrompt`, project, session IDs, and
unknown values are skipped. A bounded raw input byte buffer necessarily exists
while parsing; it is never persisted or echoed. Nesting is limited to 32.

For history, only a top-level numeric epoch-millisecond timestamp or ISO timestamp
with an explicit UTC offset is retained. Seconds are never guessed. A direct
report without an event feed reads only the last 16 MiB of the explicit history
file. Its first fragment and unfinished final line are skipped. This history
covers **Claude Code only**, may be stale, and does not establish desktop activity.

Project collection examines only `projects/<project>/*.jsonl`, excludes symlink
entries and nested subagents, examines at most 1,000 directory entries, and selects
up to 64 most recently modified eligible logs. Modification must be at least the
monitor start. The first read tails at most 1 MiB per file; later reads use private
offsets, at most 2 MiB per file and 16 MiB across all sources per invocation.
Metadata lines above 512 KiB are discarded with progress retained and a gap
counter. Short incomplete final lines are retried next time. Truncation below the
cursor or changed file identity resets to a bounded tail and records a gap.
Keyed fingerprints of at most 256 prefix bytes and 256 bytes before the cursor
also detect common truncation/regrowth rewrites that retain file identity.
File reads reject symlinks, Windows reparse points and files with multiple hard
links, checking ancestor components and the opened handle. Input names must be
JSONL logs; known auth/config/credential filenames are excluded. Collection
checks a 22-second soft work budget; the native timeout is the hard bound.
These limits intentionally trade completeness for bounded work. Rewrites outside
the bounded fingerprint regions may evade detection.

Project events require `type: "user"`, `isMeta` and `isSidechain` not true,
`message.role: "user"`, and content either a string or a nonempty array solely of
text blocks. Tool-result records are excluded. Accepted events are **human prompt
candidates**, not proven human actions. Only timestamps in the monitor range up
to the current time/deadline are appended. Injected or duplicated candidates are
possible. Cursor eviction or a crash between feed flush and cursor replacement
can repeat metadata; counts are retained records, not distinct prompts or calls.

## Local outputs and interpretation

The helper appends only whitelisted timestamps, fixed kind/provenance labels and
numeric coverage counters to `events.jsonl` (maximum 16 MiB). It keeps a bounded
private `.event-cursors.json` containing opaque HMAC path identifiers, keyed
continuity fingerprints, offsets and file identity for incremental reading.
The per-run random key stays in that same private state; no raw source paths,
filenames or source bytes are stored there. Keep that file private. Offsets and session IDs
are never copied into the reports. The native caller supplies a process memory
cap and 30-second helper timeout; this script is not a resident service.

Reporting prefers the collected feed whenever it exists, even if empty; it does
not silently rescan project logs or replace an empty feed with different coverage.
Events are always restricted to the actual sampled window; `--since-start`
explicitly also clamps to native monitoring start. At most 100,000 in-window
events are retained, and the correlation table shows at most 100. Each side of a
correlation must be within 120 seconds. Unknown sides are marked unavailable.

Each derived file is atomically replaced independently:

- `report.md`: aggregate counters, coverage, timestamp correlation and limitations.
- `report.html`: the same report with a self-contained static SVG plot; private
  commit and working set are separate series. No scripts or external resources.
- `resume.txt`: a conservative bullet with sample-supported observed Claude
  wall-clock hours, not proof of productivity or model activity.
- `manifest.json`: fixed safe fields, aggregate measurements and coverage counts;
  no raw PIDs, paths, identities, offsets, session IDs or prompt text.

`samples.jsonl` is never overwritten. The entire private output directory remains
local; the helper does not set Windows ACLs. Derived reports are not a substitute
for reviewing a file before sharing it.

Private bytes measure private commit, **not physical RAM**. Member working sets
can double-count shared pages. Reported descendants are best-effort observations,
not kernel ownership. Missing processes and partial snapshots give lower bounds.
Memory before monitoring is unknown. Sleep and sampling gaps are unknown, never
zero; plot lines break across partial samples and gaps greater than twice the
cadence. Empty partial snapshots are not plotted as zero.

Observed hours sum adjacent complete snapshots sharing a root identity, separated
by at most twice the cadence. This assumes presence between snapshots. CPU deltas
require the same root PID and creation time and nonnegative counters. Root changes
and PID reuse are not joined. Native collector peak memory and CPU are separate;
those counters do not measure short-lived Python helpers. Focus does not prove
model activity. Correlations are observational, not causal; no reduction,
ownership, recovery, or historical-memory reconstruction claims are supported.
Zero event records do not prove idle time.

## Synthetic validation

```powershell
python -B -m unittest discover -s observer -p 'test_*.py' -v
```

Tests use disposable synthetic fixtures only; they do not read real histories,
projects, accounts, or credentials. They cover escaping and skipped sensitive
strings, bounds, incomplete and oversized records, PID reuse, root changes, sleep
gaps, exact counters, absent Claude/history, incremental cursors, rotation, event
provenance, private output fields, helper diagnostics and live status.

## Finite background observation on Windows

```powershell
.\observer\build.ps1
.\observer\start.ps1 -Until ([DateTimeOffset]::Parse('2026-10-27T19:00:00+05:30'))
# Optional early stop, affecting only this observer:
.\observer\stop.ps1 -Data 'C:\private-run-folder-returned-by-start'
```

The launcher creates a fresh folder under local AppData with access limited to
the current user and SYSTEM, freezes the binary/reporter in its private runtime
folder, records exact hashes and starts hidden. Every five seconds the native
collector queries process counters; every minute a bounded helper extracts event
metadata. The collector stops at the earlier of the wall deadline or its original
elapsed duration and then generates the final report offline. This is not an
autostart service: reboot/sign-out ends it, and sleep creates an honest coverage
gap. Nothing launches, controls, suspends or terminates the existing Claude app.
Only the observer's own Python helpers are placed in its cleanup Job Object.
The sampler excludes its own process tree, even if started beneath Claude, and
requires queried members to belong to its own user SID and Windows session.
