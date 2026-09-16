# coglib

Shared Python package providing base config, database, logging, LLM access, and utilities for all COG projects.

## What belongs here

coglib is **installed into every deployed container image** (comar's
`make stage-libs` copies it into the Docker build context at build time; the
vendored `server/coglib/` copy was removed 2026-09-02), so the bar is
deliberately high:

> Something **two or more projects import**, that **runs in production**, and
> whose dependencies are cheap enough to ship everywhere.

That last clause does real work — a dependency added here is a dependency in
every image. `llm.py` talks to three providers over plain `httpx` rather than
pulling in `anthropic` + `openai` + `google-genai` for exactly this reason.

What does *not* belong here:

| Kind of thing | Where it goes |
|---|---|
| Machines, reverse proxy, backups, Proxmox/NAS topology | `infra/` — not importable code at all |
| Logic only one project needs | that project (e.g. `comar/server/backend/app/integrations/`) |
| Tools **you** run at a terminal — benchmarks, one-off analyses, probes | a workbench repo, **not** here. A bench harness in a deployed image is dead weight |

The `llm` module is the boundary case worth understanding: the **call layer**
(keys, model registry, cost accounting) is production code many projects want,
so it lives here. A **model-comparison bench** built on top of it is something
you run by hand, so it does not.

## Tech Stack

- Python 3.11+, Hatchling build
- Pydantic v2 + pydantic-settings (config)
- SQLAlchemy 2.0 (database)
- PostgreSQL via psycopg2-binary
- httpx (LLM providers — no provider SDKs)

## Structure

```
src/coglib/
├── config.py   # CogSettings base class (subclass per project)
├── db.py       # Database class, Base model, session context manager
├── llm.py      # One call() across Anthropic / OpenAI / Google + cost accounting
├── logging.py  # Structured logging setup
└── utils.py    # parse_date() with multi-format fallback
```

## coglib.llm

```python
from coglib.llm import call

r = call("claude-opus-5", "What's in this photo?", image="dog.jpg")
print(r.text, r.cost, r.secs)
```

```bash
python -m coglib.llm --list                      # models, rates, which keys exist
python -m coglib.llm opus "explain X"            # aliases: opus/sonnet/haiku/flash/gpt/...
python -m coglib.llm flash "what is this" --image x.jpg --json
cat notes.md | python -m coglib.llm haiku -      # '-' reads stdin
```

**Keys** are read from `~/.config/comar/{anthropic,openai,gemini}_api_key`,
falling back to `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`.
Files win: env vars are invisible to GUI-launched processes and are lost
between shells — the same reason comar's bearer token lives in `~/.zshenv`.
Override the directory with `COGLIB_KEY_DIR`.

### The two things that are genuinely hard

**1. Reasoning tokens are reported three incompatible ways.** This, not the
HTTP call, is why the module exists:

| Provider | Field | Relationship to output tokens | So we |
|---|---|---|---|
| Google | `thoughtsTokenCount` | **separate** counter | **add** it |
| OpenAI | `reasoning_tokens` | **nested inside** `output_tokens` | **subtract** it |
| Anthropic | — | not reported at all | report 0 |

Dropping Google's made a cost estimate **4× too low** (reproduced live:
$0.00036 vs the true $0.00167 on one reasoning prompt). Anthropic's `0` means
*not reported*, **not** *did not think* — Opus 5 and Sonnet 5 think by
default, and `cost` is still right because `output_tokens` already includes it.

**2. Only Google accepts HEIC/HEIF.** A straight-from-phone iPhone photo is a
hard skip on two of three providers. That raises `UnsupportedInput` naming the
provider and mime rather than a 400 from deep in a stack trace. Note also that
`mime_for` returning a type is *not* permission to send it — `.xyz` maps to
`chemical/x-xyz`, so membership of the provider's accepted set is the real check.

**Rates in `MODELS` are hand-maintained** (no provider exposes pricing over its
API). A stale rate silently yields a wrong `cost` — check the table when a bill
surprises you.

### Adding a model

Add a `ModelSpec(provider, $/M in, $/M out)` to `MODELS`. A test asserts every
alias resolves to a real entry, so a typo fails in CI rather than on someone
else's machine. Prefer a dated snapshot id where one exists — the bare
`claude-haiku-4-5` alias is absent from some accounts' `/v1/models` list.

## Usage

```bash
pip install -e /path/to/coglib        # Editable install
pip install -e "/path/to/coglib[dev]" # With dev tools
```

## Development

```bash
pytest          # Run tests
ruff check src/ # Lint
```

## Key Patterns

- Projects subclass `CogSettings` and set `env_prefix` in `model_config`
- Projects define models inheriting from `coglib.db.Base`
- `Database.session()` is a context manager that auto-commits/rollbacks
- `get_database()` returns a cached global instance
