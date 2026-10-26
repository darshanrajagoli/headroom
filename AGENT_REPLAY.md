# Actual agent CLI testing, without paid inference

The installed Codex and Claude command-line apps were each tested twice:
normal completion and a deliberate crash of their newly owned Headroom
controller. All four final cases passed. The fixture purchased nothing and
sent nothing to a real model service. No real credentials were supplied.

## What this proves

These were the actual installed executables, not programs renamed to look like
agents. A loopback HTTP server returned scripted Responses/Messages events.
The real agent harness used its real shell tool to launch one fixed Python
child:8MiB scratch, a3-second hold and a deterministic completion marker.

The fixture pinned that child by HANDLE and verified membership in the known
Headroom ancestor Job Object while it was alive. The actual ancestor's kernel
commit ceiling was1GiB. The task's declared hard cap was768MiB. Claude's child
reported that immediate cap; Codex added a nearer uncapped job. A nearer cap of
zero does not remove an ancestor's limit; we do not claim to have directly
queried Codex's unnamed Headroom task job from the observer.

Both normal cases required complete native evidence, successful provider JSONL
terminal events, exact final marker, and actual child completion/exit. Both
crash cases killed only the exact controller created by the fixture, verified
the real tool child exited before its work completed, and refused to classify
the interrupted run as completed. No checkpoint or automatic agent resume is
claimed. Every outer fixture drained before its handle closed.

See [consolidated evidence](evidence/agent-cli-replay.json). It embeds the four
structured case records, original record hashes, installed CLI hashes, source
hashes, request summaries and cleanup. It is not independent certification.

## How spending was avoided

- Only numeric127.0.0.1 provider URLs are accepted. The fixture has no forwarding
  implementation, model weights, real key, paid endpoint or billing account.
- The subprocess environment is allowlisted, with fresh process-only auth,
  config and profile directories. The parent environment/settings are unchanged.
- Codex skips user config/rules; the isolated home also prevents reuse of saved
  authentication. Claude uses bare mode, no discovered hooks/MCP, and a dummy
  key. Updates, telemetry and nonessential traffic are disabled for the run.
- External metadata CONNECT attempts from Codex were observed at the local
  proxy and denied, not forwarded. This proxy is not an OS-enforced network
  sandbox, and Job Objects are not a filesystem security boundary.
- Usage numbers in scripted responses are zero and are not real inference
  measurements. Client-reported cost is not used as proof of safety or success.

## Reproduce locally

Requires the existing Windows/Python/native build plus trusted locally installed
CLI executables. No install/update, login, key setup or global setting is needed.
The fixture does not download or package Codex/Claude. From the checkout:

```powershell
python tools/agent_replay.py --go --provider codex
python tools/agent_replay.py --go --provider claude
python tools/agent_replay.py --go --provider codex --crash-owner
python tools/agent_replay.py --go --provider claude --crash-owner
```

Runs are serial and bounded:45s native session,55s outer wait plus bounded
cleanup,1.5GiB outer commit ceiling and48 concurrently active processes. These
are ceilings, not allocation targets; only8MiB of fixture scratch is allocated.
State, raw requests, command arguments and failure diagnostics stay in ignored
test-output/ar-* directories. Safe structured records get new evidence names.

The final observed CLIs were Codex0.163.0-alpha.10.2 and Claude2.1.310; the
earlier inventory saw Claude2.1.306 before the installed binary changed between
runs. Each case pins its executable hash and refuses an in-run change. Future
versions may need explicit schema updates; unknown events fail closed.

Actual integration exposed and tested the empty-MCP wrapper requirement,
Claude's Bash tool name and exact SDK beta query, additive stream metadata,
Codex's isolated-catalogue warning, and asynchronous outer-job accounting.
The exact known metadata warning is surfaced as a diagnostic, not a blanket
exception for provider errors. Tool output is always data, never instructions.

## Still not proven

This is controlled protocol replay, not a test of model intelligence, useful
coding quality, paid/subscription behavior, existing desktop sessions, external
brokers/VMs/remote workers, generic session recovery or another Windows host.
Each consolidated set binds one native controller binary. Separate idle
measurements remain controller-only, not measurements of these agent apps and
their workers. Real inference/account use remains out of scope for this fixture.

Implementation choices follow the official [Codex noninteractive interface](https://learn.chatgpt.com/docs/non-interactive-mode),
[Codex provider configuration](https://learn.chatgpt.com/docs/config-file/config-advanced),
[Codex state-directory setting](https://learn.chatgpt.com/docs/config-file/environment-variables),
[Claude bare/headless mode](https://code.claude.com/docs/en/headless),
[Claude environment controls](https://code.claude.com/docs/en/env-vars), and
[Claude gateway routing](https://code.claude.com/docs/en/llm-gateway-connect).
Installed CLI help and actual observed streams, not documentation alone,
determined the exact tested recipes.
