# Security policy

comar-oss is a self-hosted personal project. There's no bug bounty and no SLA, but security reports are taken seriously and prioritized over other issues.

## Reporting a vulnerability

Please don't open a public GitHub issue for a security vulnerability. Instead, use [GitHub's private vulnerability reporting](https://github.com/cograda/comar-oss/security/advisories/new) (Security tab → Report a vulnerability). This opens a private discussion with the maintainer before anything is public.

Include what you'd normally include in a report: affected file/endpoint, the concrete impact (what an attacker could actually do), and steps to reproduce if you have them.

## Scope

In scope: the FastAPI backend, the client daemon, the WhatsApp bridge, and the deployment configs (Dockerfiles, docker-compose) in this repository.

Out of scope: the WhatsApp Terms-of-Service risk inherent to using an unofficial client library (see the README's Legal section) — that's a known, accepted tradeoff of the integration, not a bug to report. Also out of scope: the shared `HOME_UI_TOKEN` dashboard having no per-user access control — this is a deliberate design choice for a single-household deployment, documented in `server/CLAUDE.md`.

## Supported versions

There's one moving `main` branch, no maintained release branches. Fixes land on `main`; there's no backport policy.
