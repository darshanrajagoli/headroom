# Comparison and intended difference

Documentation/source inspection on 2026-10-23, not a reproduced competitive
benchmark. Star counts are deliberately not quality or novelty criteria.

| Existing work | Already does | What this prototype does NOT get to claim |
| --- | --- | --- |
| [MemMux](https://github.com/sumanyumuku98/MemMux) | Agent memory attribution, admission, priorities, cleanup, checkpoints and a benchmark; inspected platform code implements Linux/macOS and rejects others | That local AI-agent RAM governance or checkpoint-aware reclamation is new |
| [AgentCgroup](https://github.com/eunomia-bpf/agentcgroup) | Agent/tool-call resource control with cgroup/eBPF mechanisms | That hierarchical agent budgets or resource-intent policies are new |
| [Process Governor](https://github.com/lowleveldesign/process-governor) | Native Windows process/job commit caps and other controls | That Windows process-tree limits are new |
| [Pueue](https://github.com/Nukesor/pueue) | Persistent cross-platform queues, order, concurrency, groups and pause/resume | That a command queue or saved job list is new; pause does not imply freed RAM |
| [WAVA](https://github.com/brutusin/wava) | Linux memory-based batch scheduling, priority groups, child jobs and starvation handling | That memory-aware queues, job hierarchy or preemption are new |
| [qex](https://github.com/stephenc/qex) | Shared claims/admission across independent local agents | That unrelated agents can share one resource budget only with our tool |
| [systemd-oomd](https://www.freedesktop.org/software/systemd/man/258/systemd-oomd.html), [earlyoom](https://github.com/rfjakob/earlyoom) | Established Linux pressure handling and intervention | That monitoring RAM and selecting something to stop is a new invention |
| [VCode](https://v-code.dev/docs/agents-and-sessions/memory-headroom-and-idle-parking) | Admission headroom and selective idle-agent parking | That memory-pressure-aware agent parking is new |

MemMux is unusually close prior art. Its
[paper](https://arxiv.org/abs/2610.07257) and
[platform fallback](https://github.com/sumanyumuku98/MemMux/blob/main/crates/memmux-metrics/src/platform/unsupported.rs)
make it a mandatory comparison, not something to ignore because it is small.
Do not restate its published measurements as independently verified here.

## Selected first difference to test

Native Windows operation with kernel-owned descendant containment, cooperative
application checkpoints, separate hard allocation denial, durable attribution,
and low measured overhead. This is an engineering differentiation hypothesis,
not a claim of first invention. The prototype is only a backend; product value
depends on a safe shared priority/admission layer and real workload validation.

Microsoft documents the relevant existing primitives:
[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[committed-memory caps](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information),
[soft notifications](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_notification_limit_information),
and [violation evidence](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_limit_violation_information).
The code uses documented notification evidence, not an invented RSS metric or
the undocumented current-job-memory query class.
