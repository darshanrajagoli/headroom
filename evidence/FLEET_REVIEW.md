# Independent fleet review and integration fixes

Local review on 2026-10-23 by a separate agent. This is not external security
certification. No public repository or third party was contacted.

## Findings fixed before the final build

- P1: consecutive admissions reused the prior fleet/host measurement. New
  tasks initially had zero measured commit. Admission now starts at most one
  task per complete sample cycle, so every subsequent decision sees fresh
  membership, usage and host availability. Claims remain estimates, not a
  reservation of physical pages or a promise about future allocation.
- P1: a failed first fresh pressure check retained the mature pressure timer.
  Recovery or an invalid observation could therefore allow a later pressure
  return to skip the sustain interval. Both failed rechecks now reset continuity.
- P2: the binary parser accepted unpaired UTF-16 surrogates. It now validates
  pairs before creating any run state; valid supplementary characters remain
  supported.

The reviewer checked these exact changes again and found no remaining blocker
in those paths or the portable policy implementation. The offline client's
52 regressions passed at its final checkpoint.

## Additional integration findings

- A process launch failure previously aborted the whole fleet. It now records
  immutable `launch_failed` code70, cleans only the new failed task, and leaves
  peers running. Failure to confirm cleanup or record the outcome still closes
  the owned fleet. The client and recovery tool recognize the new outcome.
- A task may flush an acknowledgment and exit before the asynchronous ack
  reader finishes. Empty stopped groups now retain their original grace window
  to collect that acknowledgment. Grace is not extended and an empty group is
  not killed.
- Windows exit259 is a legitimate exit after the kernel reports no active
  processes; it is no longer confused with a still-running process.
- Error, deadline and interrupt paths confirm owned process-tree cleanup under
  a shared five-second bound, including an unassigned suspended child.
- The recovery wrapper initially relied on an unavailable PowerShell hashing
  command. Its real crash/recovery integration failed; it now uses .NET SHA256
  directly. All three wrapper tests then passed.

Use `native-tests.json` and `fleet-tests.json` for the final integrated binary,
counts and source hashes. Do not add review-only counts to the aggregate total.

## Unproven boundaries

Tests do not prove real-time operation, physical-RAM safety, arbitrary application
checkpoint correctness, power-loss atomicity or security against the same user.
Direct injection of every Windows API/storage failure remains incomplete.
Finite batches are not a live, independent-session submission service. No real
Claude/Codex/browser checkpoint adapter has been validated.
