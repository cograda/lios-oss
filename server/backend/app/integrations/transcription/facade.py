"""transcription's facade — capability `transcription.audio`.

Two methods, because callers genuinely need both:

  - `available()` — is transcription usable at all? Callers driving a queue need
    to distinguish "no key configured, skip quietly" from "configured but this
    file failed". Without this the inbox cron would either log an error every
    five minutes on an unconfigured deployment, or swallow real failures.
  - `transcribe()` — do it, and say where the text came from.

`prefer` exists because the cheapest transcript is the one already inside the
file. Apple embeds an on-device transcript in memos recorded on an Apple device
(see `embedded.py`), which is free and instant:

  - anything other than `"embedded"` (the live path) — pay for the better,
    speaker-labelled result; fall back to the embedded transcript only if the
    provider is unavailable or fails, so a network blip still yields something
    rather than nothing. The historical value here is `"openai"`, kept working
    as the default for backward compatibility — see below, it no longer
    selects anything.
  - `prefer="embedded"` (backfill path) — use the free transcript wherever one
    exists and spend only on the genuine gaps. Over this vault's 373 memos that's
    the difference between paying for ~70 files and paying for ~342.

**Gemini-only, owner decision 2026-08-26** (see `vault/Projects/lios/Plans/
AI Broker — Role Registry and Usage Ledger.md`, "Decided: transcription is
Gemini-only"). The evidence was already in this file before the decision:
measured head-to-head against `gpt-4o-transcribe-diarize` on a real memo
(2026-08-07), `gemini-3.6-flash` was more accurate, ~40% faster, and
cheaper — while OpenAI's diarizing model invented two speaker labels on a
single-speaker recording. The OpenAI path (`client.py`, `DIARIZING_MODELS`,
and the provider-selection branching this facade used to do) is deleted, not
kept as a fallback: `_resolve_provider`'s old default silently preferred
OpenAI whenever both keys were set, which is the shape of failure the plan
calls out by name — a compatibility default outliving the comparison that
should have retired it. The paid path now resolves through the role
registry (`app.services.ai_roles`, role `stt.memo`), which is the one place
"what model transcribes memos" is answered. Cloud Speech-to-Text (Chirp) was
considered and rejected: different auth (GCP service account, not the AI
Studio key this codebase already holds for embeddings and vision) and
diarization only via GCS-backed batch jobs.

The `embedded` (.tsrp) fallback is untouched by this — it is a no-model
path (whatever the recording device already transcribed on-device), not a
provider, so it isn't part of the role registry at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.errors import ComarError, TransientError
from app.plugin.config_store import plugin_config
from app.services import ai_roles

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptResult:
    """Outcome of a transcription attempt.

    `text` empty with `error` set is a failure; `text` empty with no `error`
    means the audio genuinely contained no speech (a pocket recording), which
    the caller should record so it isn't retried forever.

    `transient` (added for the Tines-retry migration): true iff `error` came
    from a `TransientError` — a 5xx, timeout, or rate limit that is likely to
    succeed if tried again — as opposed to a `PermanentError` (bad request,
    oversized file, unsupported container) or "no provider configured", where
    retrying changes nothing. Defaults to False so every existing failure
    path (including "no provider configured") is treated as permanent unless
    a caller explicitly marks it otherwise — the safe default, since retrying
    a genuinely permanent failure forever is the bug this field exists to
    prevent callers from reintroducing.
    """

    text: str
    source: str  # "openai" | "gemini" | "embedded" | "none"
    model: str | None = None
    error: str | None = None
    # Populated only by the `gemini` source's structured-output response
    # (see `gemini.py`'s `GeminiTranscript`); `None` for every other source
    # and whenever Gemini's response didn't parse as the requested JSON
    # shape. `title` is deliberately name-free — see `gemini.py`'s
    # `_BASE_PROMPT` docstring on why: it's rendered on a phone lock-screen
    # notification and in an email subject line.
    title: str | None = None
    speakers: int | None = None
    # Whether `error` is worth retrying. This is NOT the same question as
    # `ok` — the caller's decision it feeds is whether to stamp the attempt
    # as terminal (`inbox/scan.py`'s `transcribed_at`). Note that empty
    # `text` with no `error` at all is a *terminal* success-shaped outcome
    # (silence, a pocket recording), so `transient` stays False there and the
    # file is never retried forever.
    transient: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.text)


class TranscriptionFacade:
    def available(self) -> bool:
        """Whether a paid transcription can currently be attempted.

        Gemini-only: true iff `transcription.gemini_api_key` is set. The role
        registry (`stt.memo`) governs *which model*; the key check stays here
        because `RoleNotBoundError` is about the model string being empty,
        not about whether a credential exists — the two failure modes need
        different messages for an operator.
        """
        try:
            cfg = plugin_config("transcription")
            return bool((getattr(cfg, "gemini_api_key", "") or "").strip())
        except Exception:  # noqa: BLE001
            # Config table unreachable — report unavailable rather than raising
            # into a caller's queue loop.
            logger.exception("[transcription] config lookup failed")
            return False

    def transcribe(
        self,
        path: Path,
        *,
        prefer: str = "gemini",
        vault_root: Path | None = None,
        duration_s: float | None = None,
    ) -> TranscriptResult:
        """Transcribe `path`. Never raises — failures come back as a result.

        Callers are queue walkers processing many files; one bad file must not
        abort the batch, and the reason has to be recordable against that file.

        `prefer="openai"` is still accepted (old callers may still pass it)
        and behaves exactly like the default — Gemini is the only paid
        provider now, so there is nothing left for that value to select.
        """
        from app.integrations.transcription import dictionary, embedded, gemini

        if prefer == "embedded":
            text, _locale = embedded.read_embedded_transcript(path)
            if text:
                return TranscriptResult(text=text, source="embedded")

        cfg = plugin_config("transcription")
        api_key = (getattr(cfg, "gemini_api_key", "") or "").strip()

        provider_error: str | None
        # Mirrors `provider_error`: whether that error is worth retrying. Only
        # a caught `TransientError` sets this True; every other branch here
        # (bad role binding, `PermanentError`, an unexpected exception, no key
        # configured at all) leaves it False, so a caller who ignores this
        # field entirely still gets today's "treat as permanent" behaviour.
        provider_error_transient = False
        if api_key:
            try:
                binding = ai_roles.resolve_stt_memo()
            except ai_roles.RoleNotBoundError as exc:
                # Loud (ERROR, not debug — see ai_roles.py's module docstring
                # on why "loud" here means "clearly logged", not "raises":
                # transcribe()'s own contract is never to raise into a queue
                # walker).
                logger.error("[transcription] role 'stt.memo' is not bound: %s", exc)
                provider_error = str(exc)
            else:
                model = binding.model
                prompt = dictionary.build_prompt(
                    vault_root if cfg.dictionary_from_vault else None,
                    cfg.extra_dictionary_terms or [],
                )
                try:
                    parsed = gemini.transcribe(
                        path,
                        api_key=api_key,
                        model=model,
                        prompt=prompt,
                        max_file_mb=getattr(cfg, "gemini_max_file_mb", None) or 18,
                        duration_s=duration_s,
                    )
                    return TranscriptResult(
                        text=parsed.text,
                        source="gemini",
                        model=model,
                        title=parsed.title,
                        speakers=parsed.speakers,
                    )
                # Must precede the `ComarError` clause below: TransientError is
                # a ComarError, so ordering is what makes the distinction exist
                # at all. Swapping these two is a silent regression — every
                # retryable failure would be recorded as terminal.
                except TransientError as exc:
                    logger.warning("[transcription] %s failed (transient): %s", path.name, exc)
                    provider_error = str(exc)
                    provider_error_transient = True
                except ComarError as exc:
                    logger.warning("[transcription] %s failed: %s", path.name, exc)
                    provider_error = str(exc)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[transcription] %s raised unexpectedly", path.name)
                    provider_error = str(exc)
        else:
            provider_error = "no transcription provider configured"

        # Last resort: whatever the device already transcribed. Better a rougher
        # transcript than none — and for a caller that already paid the upload
        # cost of getting the file here, silence is the worst outcome.
        text, _locale = embedded.read_embedded_transcript(path)
        if text:
            logger.info("[transcription] %s fell back to embedded transcript", path.name)
            return TranscriptResult(text=text, source="embedded", error=provider_error)

        return TranscriptResult(
            text="", source="none", error=provider_error, transient=provider_error_transient,
        )


FACADE = TranscriptionFacade()
