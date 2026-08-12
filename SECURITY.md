# Security Policy

## Supported versions

Security fixes land on the latest released version of `horus-runtime`. Please upgrade to
the newest release before reporting an issue.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Anything older | No |

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately in either of these ways:

- [Open a private security advisory](https://github.com/temple-compute/horus-runtime/security/advisories/new)
  on this repository (preferred).
- Email [christian@templecompute.com](mailto:christian@templecompute.com).

Please include:

- What the issue is and the impact you believe it has.
- Affected version (`horus --version`) and platform.
- Steps to reproduce, ideally a minimal workflow file.

## What to expect

- We aim to acknowledge a report within **3 working days**.
- We will confirm the issue, agree a fix and a disclosure timeline with you, and keep you
  updated while we work on it.
- With your permission we will credit you in the advisory when the fix is released.

Please give us a reasonable window to ship a fix before disclosing publicly.

## Scope note

Horus Runtime executes the commands and code that a workflow file tells it to execute.
Treat a workflow file as you would a shell script: **only run workflows you trust**. A
workflow doing something harmful because its author wrote it that way is not a
vulnerability in the runtime. Sandbox escapes, privilege escalation beyond what the
workflow declares, and unintended remote code execution are.
