# Security policy

This is an experimental local prototype, not a security sandbox. There is no
supported production/LTS version or promised response time. Only the current
source is maintained on a best-effort basis.

Read SAFETY.md first: same-user workloads can access state, commit is not
physical RAM, and external brokers/services may escape process ancestry.
Do not submit hostile or untrusted code assuming resource caps isolate it.

For a sensitive report, use [GitHub private vulnerability reporting](https://github.com/darshanrajagoli/headroom/security/advisories/new),
which is enabled for this repository. If unavailable, open a minimal public
issue asking for a private channel without posting exploitation details or data.
Do not post credentials, real argv, raw state/logs or personal histories.
Use synthetic reproduction data only. No security audit or certification is
implied by the project's tests or AI-assisted reviews.
