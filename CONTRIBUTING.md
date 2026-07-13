# Contributing

Thanks for considering a contribution. comar-oss is maintained by one person in their spare time, so a few expectations up front:

- **Response time**: issues and PRs get reviewed, but not on a fixed schedule. Don't expect same-day turnaround.
- **Scope**: this project runs a real household — bug fixes, new integrations, and documentation improvements are all welcome. Larger architectural changes (auth model, transport, multi-tenancy) are worth opening an issue to discuss before you put work into a PR, since they might not fit how this is actually used.
- **By submitting a PR**, you agree your contribution is licensed under this repository's [MIT license](LICENSE).

## Before you open a PR

- Check the existing issues and PRs for overlap.
- For anything beyond a small fix, open an issue first describing what you want to change and why — this saves you from doing work that doesn't get merged.
- Run the test suite and keep it green:
  - `cd server/backend && pytest -m "not db"` (fast, no Docker needed) and `pytest -m "db"` (needs Docker, spins up a real Postgres via testcontainers)
  - `cd client && pytest`
  - `cd server/frontend && npm run build` (type-checks as part of the build)
- Match the existing code style — there's no separate style guide beyond what's already in the codebase.
- Keep PRs focused. A PR that fixes one bug or adds one integration is much easier to review than one that does several unrelated things.

## Adding a new integration

See `server/CLAUDE.md`'s "Adding an Integration" section for the five-file pattern (`__init__.py`, `client.py`, `models.py`, `sync.py`, `tools.py`) every integration follows. New integrations are one of the most useful contributions — they're additive and don't touch shared code paths.

## Reporting a security issue

Please don't open a public issue for a security vulnerability. See [SECURITY.md](SECURITY.md) instead.

## What not to send

- Changes that reintroduce personal data patterns this project deliberately avoids (hardcoded names, locations, credentials) — keep examples generic (the fictional Alex/Sam/Finn/Isla household already used throughout).
- Dependency additions under a copyleft license (GPL/AGPL/SSPL) — this project ships under MIT and needs to stay that way. Open an issue first if you think one is unavoidable.
