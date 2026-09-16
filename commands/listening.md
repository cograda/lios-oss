Analyse my recent listening using Last.fm data via MCP tools.

Arguments: $ARGUMENTS

If no arguments, default to "this week".

## How to run this

### Step 1 — Fetch data (main agent, MCP tools)

Call these MCP tools:

1. **`lastfm_stats`** — pass the period based on what the user asked for. Returns total scrobbles, top artists, top tracks, top genres.
2. **`lastfm_recent`** — get the last 50 scrobbles for session/pattern analysis.
3. **`lastfm_search`** — if the user asks about a specific artist or track, search for it.

### Step 2 — Analyse (Haiku subagent)

Spawn a **Haiku subagent** (model: haiku) with the raw MCP tool results. Give it these instructions:

> You are analysing Last.fm listening data. The raw data from the API is provided below.
>
> Analyse and return a structured summary covering:
> - **Overview**: total scrobbles, estimated listening hours (avg 3.5 min/track)
> - **Top artists**: with genres where available, highlighting heavy rotation vs. one-offs
> - **Genre breakdown**: what the overall listening mood/theme was
> - **Discoveries**: any artists with low play counts that look new
> - **Patterns**: time of day from timestamps, any notable sessions or binges (gaps > 30 min between scrobbles = new session)
> - **Vibe summary**: 1-2 sentence characterisation of the listening period
>
> Here is the data:
> [paste the raw MCP tool results here]

### Step 3 — Present results (main conversation)

Take the Haiku agent's analysis and present it conversationally. Add any context you know about the user's taste or habits.

## Period mapping

| User says | MCP period parameter |
|-----------|---------------------|
| today | today |
| this week / the week | this_week |
| this month | this_month |
| this year | this_year |
| last week | last_week |
| last month | last_month |
| all time | all_time |

## Examples

- `/listening` → this week's analysis
- `/listening this month` → last 30 days
- `/listening march` → custom date range for March 2026
- `/listening last year` → 1 year lookback
