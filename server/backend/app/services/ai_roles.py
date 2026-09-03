"""The AI role registry — W2 chunk 2.

`vault/Projects/lios/Plans/AI Broker — Role Registry and Usage Ledger.md`
§1 ("Roles, not model ids") is the spec. Callers ask for a **role**
(`"stt.memo"`, `"vision.inbox"`, `"embed.corpus"`) instead of hardcoding a
provider and model; config maps role -> binding, so swapping a model is one
config value and no caller changes.

Only the roles with a live caller today are registered here. The plan lists
`stt.command`, `tts.panel`, `chat.panel` and `face.identify` too, but those
belong to the Hall Panel / conversation-agent consumers that don't exist yet
— registering them now would be config with nothing to bind, and the plan is
explicit that a role with no binding is a loud error, not a placeholder.
Add each one in the same change as its first caller.

**Storage is deliberately not uniform**, and that is a decision, not an
oversight:

  - `embed.corpus` is a *kernel* setting (`HomeSettings.embedding_provider`,
    `HOME_EMBEDDING_PROVIDER`) — unchanged from before this chunk. It already
    supports the one case none of the others need: an ordered,
    comma-separated multi-binding list, because writing embeddings targets
    every configured space at once and search falls back through them in
    order (`app/plugin/embedding_provider.py::_configured_ids()`). This
    module does not reimplement that; `resolve_embed_corpus()` is a thin
    wrapper over `embedding_provider.get_providers()` so `_active_spaces()`
    and every existing embedding test keep working unmodified.
  - `stt.memo` and `vision.inbox` are *integration_config* — the
    `transcription.gemini_model` / `vision.model` fields that already
    existed before this chunk. The plan's letter says a generalised kernel
    setting; in practice each of those fields already carries a
    measurement-backed default and description (vision's model choice in
    particular records a 10-model, then a 4-model, comparison run) that a
    bare kernel string would either duplicate or lose. Reusing the existing
    key means "what serves role X" still has exactly one place to look —
    `ai_roles.resolve()` — even though the value lives where it always did.

**A role with no binding is a loud startup error, never a silent default.**
"No binding" means the configured value is empty — `RoleNotBoundError`
(a `RuntimeError`) is raised at resolution time, which for a config-store
field is the first call after boot, not literal process startup (the kernel
setting for `embed.corpus` already worked this way before this chunk, via
`embedding_provider._configured_ids()`'s `ValueError`).

**Divergence from the plan's letter, flagged for review:** the plan says
"a model with no rate row [in `coglib.llm.MODELS`] is a loud startup error."
`MODELS` prices chat/vision models only — it has no rows for embedding or
STT models at all (see `coglib/llm.py`), so hard-failing on that condition
would make `stt.memo` and `embed.corpus` permanently unbindable with the
plan's own initial bindings (`gemini-embedding-2`, `BAAI/bge-small-en-v1.5`
have no entry; even fastembed's local model never will, since it's free).
This module logs **one warning per (role, model) per process** instead, and
lets the call proceed with `cost_usd=None` (unknown, not free) — the
"null-means-unknown" rule the ledger itself already applies. Hard-failing
here is a straightforward follow-up once `coglib.llm.MODELS` grows non-chat
rows, if that's still wanted.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class RoleNotBoundError(RuntimeError):
    """A registered role has no binding configured.

    Loud by design: per the plan, a role with no binding must never fall
    back to a silent default. Callers should let this propagate (it means
    the deployment is misconfigured), not catch-and-guess.
    """


@dataclass(frozen=True)
class RoleBinding:
    """What currently serves one role.

    `provider` is a coarse label matching the values already written into
    `ai_usage.provider` elsewhere in this codebase (`"google"` for every
    Gemini call, `"local"` for on-box inference with no vendor at all) —
    not a `coglib.llm` provider id, and not the embedding registry's
    `provider_id` strings (`"fastembed-bge-small"`, `"gemini-embedding-2"`),
    which name a specific model+backend pairing rather than a vendor.
    """

    role: str
    provider: str
    model: str


#: Every role with a live caller today. Adding a role here with no caller
#: wiring it in is exactly the "dead config" the chunk brief warns against —
#: don't.
ROLES = frozenset({"stt.memo", "vision.inbox", "embed.corpus"})

_warned_missing_rate: set[str] = set()
_warn_lock = threading.Lock()


def _warn_if_unrated(role: str, model: str) -> None:
    """Log one warning if `model` has no rate row in `coglib.llm.MODELS`.

    See the module docstring's "Divergence from the plan's letter" section —
    this is a warning, not a hard failure, because `MODELS` prices chat/
    vision models only and several live role bindings (embeddings, local
    inference) will never appear in it. Deduplicated per (role, model) per
    process so a hot path doesn't spam the log on every call.
    """
    key = f"{role}:{model}"
    with _warn_lock:
        if key in _warned_missing_rate:
            return
        _warned_missing_rate.add(key)

    try:
        from coglib import llm as _llm

        if model not in _llm.MODELS:
            logger.warning(
                "ai_roles: role %r is bound to model %r, which has no rate "
                "row in coglib.llm.MODELS (it prices chat/vision models "
                "only) — costs recorded for this role will be NULL "
                "(unknown, not free) until a rate is added. See "
                "app/services/ai_roles.py's module docstring.",
                role, model,
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "ai_roles: rate-table check failed for role=%r model=%r",
            role, model, exc_info=True,
        )


def resolve_stt_memo() -> RoleBinding:
    """`stt.memo` — voice memos, meeting audio. Gemini-only (owner decision,
    2026-08-26 — see the plan's "Decided: transcription is Gemini-only").

    Storage: `transcription.gemini_model` (`app/integrations/transcription
    /manifest.py`), default `"gemini-3.7-flash"` (bumped 2026-08-14: half
    the cost and latency of `gemini-3.6-flash` with no quality loss on a
    benched real recording — see the manifest field's own description for
    the full bench note, including why `gemini-3.5-flash-lite` is excluded).
    `gemini-3.6-flash` was itself the model the facade's docstring records
    as more accurate, faster and cheaper than the retired OpenAI path.
    """
    from app.plugin.config_store import plugin_config

    cfg = plugin_config("transcription")
    model = (getattr(cfg, "gemini_model", "") or "").strip()
    if not model:
        raise RoleNotBoundError(
            "role 'stt.memo' has no binding — transcription.gemini_model is "
            "empty. Set it via PUT /api/integrations/transcription/config."
        )
    _warn_if_unrated("stt.memo", model)
    return RoleBinding(role="stt.memo", provider="google", model=model)


def resolve_vision_inbox(*, fallback: str | None = None) -> RoleBinding:
    """`vision.inbox` — image triage in the inbox pipeline.

    Storage: `vision.model` (`app/integrations/vision/manifest.py`), default
    `"gemini-3.5-flash-lite"` — chosen by measurement (10 models x 8 real
    inbox images, re-tested 2026-08-14), not by the plan table's generic
    `gemini-3.6-flash` placeholder. Deliberately not overridden here.

    `fallback` exists only because this module lives under `app/services/`
    (kernel), and `tests/test_kernel_import_guard.py` forbids kernel code
    from statically importing one specific integration's internals —
    including its manifest — outside `<pkg>.facade`. In real operation
    `plugin_config()` already fills in the schema default whenever a stored
    value is empty (see `app/plugin/config_store.py::plugin_config()`), so
    `cfg.model` is never actually blank; `fallback` only matters for a test
    double that bypasses that filling. The caller that legitimately knows
    its own manifest default — `vision/facade.py`, not this module — passes
    it in.
    """
    from app.plugin.config_store import plugin_config

    cfg = plugin_config("vision")
    model = (getattr(cfg, "model", "") or fallback or "").strip()
    if not model:
        raise RoleNotBoundError(
            "role 'vision.inbox' has no binding — vision.model is empty. "
            "Set it via PUT /api/integrations/vision/config."
        )
    _warn_if_unrated("vision.inbox", model)
    return RoleBinding(role="vision.inbox", provider="google", model=model)


#: embedding_provider provider_id -> the ledger-facing vendor label. Kept
#: here (not on the provider classes) so embedding_provider.py stays
#: unaware that a role registry exists on top of it — the whole point of
#: "thin wrapper" is that the wrapped module doesn't need to change.
_EMBED_PROVIDER_LABEL = {
    "fastembed-bge-small": "local",
    "gemini-embedding-2": "google",
}


def resolve_embed_corpus() -> list[RoleBinding]:
    """`embed.corpus` — semantic search. The one multi-binding role.

    Thin wrapper over `app.plugin.embedding_provider.get_providers()`,
    which already implements the ordered, comma-separated, multi-space
    semantics `HOME_EMBEDDING_PROVIDER` needs (search tries each configured
    space in order; writes target every configured space). Raises whatever
    `get_providers()` raises — currently a bare `ValueError` from
    `_configured_ids()` when the setting is empty or names an unknown
    provider id, which is this role's "loud startup error, no silent
    default" the same way the other two roles' `RoleNotBoundError` is.
    """
    from app.plugin import embedding_provider as _ep

    providers = _ep.get_providers()
    bindings = [
        RoleBinding(
            role="embed.corpus",
            provider=_EMBED_PROVIDER_LABEL.get(p.provider_id, p.provider_id),
            model=p.model_name,
        )
        for p in providers
    ]
    for b in bindings:
        _warn_if_unrated(b.role, b.model)
    return bindings


_SINGLE_RESOLVERS = {
    "stt.memo": resolve_stt_memo,
    "vision.inbox": resolve_vision_inbox,
}


def resolve(role: str) -> RoleBinding:
    """Resolve a single-binding role. Use `resolve_embed_corpus()` for
    `embed.corpus`, which is multi-binding by design (see above)."""
    if role not in _SINGLE_RESOLVERS:
        if role in ROLES:
            raise ValueError(
                f"role {role!r} is multi-binding — call resolve_embed_corpus() "
                "(or its role-specific resolver), not resolve()"
            )
        raise ValueError(f"unknown role {role!r} — not in ai_roles.ROLES")
    return _SINGLE_RESOLVERS[role]()


def _reset_warnings_for_tests() -> None:
    """Test-only: clear the per-process dedup set so a test can assert a
    warning is logged without depending on suite ordering."""
    with _warn_lock:
        _warned_missing_rate.clear()
