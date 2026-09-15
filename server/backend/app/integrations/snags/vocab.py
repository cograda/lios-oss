"""Deployment-specific snag vocabulary — rooms, trades, and trade labels.

Split out on 2026-07-28 as part of separating platform code from
personalisation. These three things were hardcoded Python constants naming
one household's rooms ("Finn's room") and one project's contractors
("northgate", "sparks-co", "Electrician (Paddy)"). They are per-deployment data,
so they now come from the `snags` manifest's config keys — `integration_config`
rows, or the `HOME_ROOM_ALIASES` / `HOME_TRADES` / `HOME_TRADE_LABELS` env
fallback.

Each accessor falls back to a generic default when unconfigured, so a fresh
deployment works out of the box with no config at all.

`trades()` is the *offered* vocabulary; `allowed_trades(session)` is the
*accepted* one and unions it with whatever is already in the table. Validation
uses the latter, so narrowing or unsetting the config can never make existing
rows un-editable. When adding a trade to a live deployment, update the config
rather than editing the fallback here.
"""

from __future__ import annotations

from app.plugin.config_store import plugin_config

# Generic fallbacks. A deployment with real contractors overrides `trades`
# via config; these exist so an unconfigured install is still usable.
DEFAULT_TRADES: tuple[str, ...] = (
    "builder",
    "carpenter",
    "electrician",
    "painter",
    "plumber",
    "tiler",
    "other",
    "unknown",
)

# Structural statuses/severities are platform behaviour, not personalisation —
# they stay as code constants in models.py.

_GENERIC_LABELS = {"unknown": "Unassigned", "other": "Other contractor"}


def room_aliases() -> dict[str, str]:
    """How people actually type a room → its canonical name.

    Keys are matched lowercased. Empty by default: an unconfigured
    deployment just capitalises whatever the reporter typed.
    """
    return plugin_config("snags").room_aliases or {}


def trades() -> tuple[str, ...]:
    """Configured trade vocabulary, ignoring what's already in the table.

    Use `allowed_trades(session)` for validation — this one is for callers
    that just want to know the configured set (docs, UI hints).
    """
    configured = plugin_config("snags").trades
    return tuple(configured) if configured else DEFAULT_TRADES


def allowed_trades(session) -> tuple[str, ...]:
    """Trades accepted on write: the configured set PLUS any already in use.

    Validation must never reject a value that already exists in the table.
    Otherwise a deployment whose `trades` config is unset (or narrowed) makes
    every existing row carrying an unlisted trade permanently un-editable —
    for this deployment that would have been 149 of 198 snags, since `northgate`
    and `sparks-co` moved out of the code defaults into config.

    Union-with-reality means the config key controls what's *offered*, never
    what's *rejected retroactively*. Best-effort: if the lookup fails the
    configured set still applies.
    """
    from app.integrations.snags.models import Snag

    configured = set(trades())
    try:
        in_use = {
            row[0]
            for row in session.query(Snag.trade).distinct().all()
            if row[0]
        }
    except Exception:  # pragma: no cover - defensive
        in_use = set()
    return tuple(sorted(configured | in_use))


def trade_labels() -> dict[str, str]:
    """Display labels for trades, for the rendered vault note and the Sheet.

    Falls back to a title-cased version of the slug, so a trade added to
    `trades` config without a matching label still renders sensibly rather
    than raising or showing a raw slug.
    """
    configured = plugin_config("snags").trade_labels or {}
    return {
        slug: configured.get(slug) or _GENERIC_LABELS.get(slug) or _humanise(slug)
        for slug in trades()
    } | {k: v for k, v in configured.items()}


def _humanise(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().capitalize()


def label_for_trade(slug: str | None) -> str:
    """Display label for one trade, tolerant of values not in `trades()`.

    A snag can carry a trade that was later removed from config; render it
    rather than dropping it.
    """
    if not slug:
        return _GENERIC_LABELS["unknown"]
    return trade_labels().get(slug) or _humanise(slug)
