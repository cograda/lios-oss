"""The LLM half of the harness: `coglib.llm`, plus a cost ledger per run.

Not every algo is numeric. Some of what this layer will be asked to do is
judgement — classify a message, rank a set of options, read a photo, summarise
a week — and those belong on the same footing as a ridge regression: declared,
scheduled, recorded, and *scored*. A prompt whose output nobody grades is
exactly as untrustworthy as a model whose predictions nobody grades.

Two things this adds over calling `coglib.llm.call()` directly:

  **A ledger.** Every call's tokens and cost accumulate onto the `AlgoRun`
  row, so "what did the predictive layer cost this month" is one query. This
  matters more than the HTTP: `coglib.llm` exists because the three providers
  report reasoning tokens three incompatible ways, and dropping Google's
  separate `thoughtsTokenCount` once made an estimate four times too low.

  **Keys from comar's config store, not the filesystem.** `coglib.llm`'s
  default lookup is a key file or an env var, which is right for a hand-run
  tool and wrong for a container whose secrets live encrypted in
  `integration_config`. The key is passed as an argument (the `api_key=`
  parameter added to `coglib.llm.call` for this) rather than written into
  `os.environ` first — env mutation races between scheduler threads and leaks
  the key into anything that dumps the environment.

`ask_json()` is here because structured output is what a deriver almost always
wants, and because a model asked for JSON returns it wrapped in a code fence
often enough that every caller would otherwise write the same stripping code.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Where each provider's key already lives in `integration_config`, in
#: preference order. These are the keys the household has already configured —
#: `vision`/`embedding` hold the Gemini key, `apple_reminders` holds the
#: Anthropic one — and reusing them beats minting an `algo_*_api_key`: one
#: household has one key per provider, and a second copy of a secret is a
#: second thing to rotate and a second thing to forget to rotate.
#:
#: OpenAI is absent because nothing in comar is configured for it yet. A
#: deriver that wants an OpenAI model gets the file/env fallback, or adds the
#: key to its own manifest and a line here.
_PROVIDER_CONFIG: dict[str, list[tuple[str, str]]] = {
    "google": [("vision", "gemini_api_key"), ("embedding", "gemini_api_key")],
    "anthropic": [("apple_reminders", "anthropic_api_key")],
}


class LLMUnavailable(RuntimeError):
    """No key is configured for the provider this model belongs to.

    A distinct type so a deriver can degrade (skip the LLM step, fall back to
    its numeric model) rather than record a hard failure — the same shape as
    `commute` producing an honest degraded decision when one feed dies.
    """


def _resolve_key(provider: str) -> str:
    """Provider key from `integration_config`, falling back to file/env.

    The fallback keeps local development and `cogkit bench` working unchanged:
    on a laptop the key is in `~/.config/comar/`, in the container it is in the
    database, and neither has to know about the other.
    """
    from coglib import llm as _llm

    for integration, field_name in _PROVIDER_CONFIG.get(provider, []):
        try:
            from app.plugin.config_store import plugin_config

            value = getattr(plugin_config(integration), field_name, "") or ""
            if value.strip():
                return value.strip()
        except Exception as exc:
            # A disabled integration or an unreadable config row must not mask
            # the remaining candidates or the file/env path — log and continue.
            logger.debug(f"algo.llm: {integration}.{field_name} lookup failed ({exc})")
    return _llm.api_key(provider)


@dataclass
class LLMLedger:
    """Accumulated cost of the LLM calls made during one algo run."""

    model: str
    calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    def record(self, response) -> None:
        self.calls += 1
        self.tokens += (
            response.prompt_tokens + response.reasoning_tokens + response.output_tokens
        )
        self.cost_usd += response.cost

    def as_run_fields(self) -> dict:
        return {
            "llm_model": self.model,
            "llm_calls": self.calls,
            "llm_tokens": self.tokens,
            "llm_cost_usd": round(self.cost_usd, 6),
        }


class AlgoLLM:
    """A model, a ledger, and two ways to ask it something."""

    def __init__(self, algo: str, model: str):
        self.algo = algo
        self.model = model
        self.ledger = LLMLedger(model=model)

    def ask(self, prompt: str, *, system: str | None = None, max_tokens: int = 4096, **kw) -> str:
        from coglib import llm as _llm

        name, spec = _llm.resolve(self.model)
        key = _resolve_key(spec.provider)
        if not key:
            raise LLMUnavailable(
                f"{self.algo}: no {spec.provider} API key configured for {name} "
                f"(set it on the owning integration's config, or put it in "
                f"~/.config/comar/)"
            )
        response = _llm.call(
            name, prompt, system=system, max_tokens=max_tokens, api_key=key, **kw
        )
        self.ledger.record(response)
        logger.info(
            f"{self.algo}: {name} {response.output_tokens}out "
            f"${response.cost:.4f} {response.secs}s"
        )
        # Additive to the AlgoRun aggregation above, not a replacement: that
        # aggregation is per-run and lives on algo_runs for the algo harness's
        # own scoring/dashboard use; this is the same call landing on the
        # cross-cutting ai_usage ledger so it shows up in "what did AI cost
        # this month" alongside every other caller. Never raises — see
        # app.services.ai_ledger's module docstring.
        from app.services import ai_ledger

        ai_ledger.record_llm_response(
            response, caller=f"algo:{self.algo}", kind="chat",
        )
        return response.text

    def ask_json(self, prompt: str, **kw) -> dict | list:
        """Ask for JSON and parse it, tolerating a code fence.

        Raises `ValueError` with the raw text truncated into the message on a
        parse failure. Truncated rather than logged in full because a prompt
        response can be long and this ends up in a `SyncState` error column
        that the dashboard renders.
        """
        text = self.ask(prompt, **kw).strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
        if fenced:
            text = fenced.group(1)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{self.algo}: model did not return JSON ({exc}): {text[:300]}")
