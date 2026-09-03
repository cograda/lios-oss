"""The AI usage ledger's write path — fire-and-forget, buffered, never blocking.

`vault/Projects/lios/Plans/AI Broker — Role Registry and Usage Ledger.md`
§3 states the constraint this module exists to satisfy: a voice reply has a
~1-2s budget, so a comar outage, a slow disk, or a locked table must never add
latency to a spoken answer, and must never fail the call it is describing.

The shape that follows from that:

  `record()` never raises and never blocks on the database. It appends to a
  small in-memory ring buffer (bounded, so a dead database can't grow this
  process's memory without limit) and returns immediately. A lazily-started
  daemon thread drains the buffer on an interval and does the real INSERT.

  **Dropped rows are counted, not silently discarded.** `dropped_count()`
  exposes the running total, and a warning is logged on every flush cycle
  that dropped at least one row since the last flush. This is the same shape
  as the copy-and-verify convention's blocklist rule (`Code/CLAUDE.md`): a
  meter that reports success while quietly losing data is worse than no
  meter at all, because it looks trustworthy.

This module intentionally does NOT use the `background_tasks`/`TaskSpec`
machinery in `app.plugin` — that system supervises per-integration asyncio
tasks tied to the FastAPI event loop, and the whole point of "never adds
latency" is that a ledger write must not depend on that loop being free (a
slow synchronous tool handler, a blocked SSE generator). A plain daemon
thread with its own lock is simpler and has no dependency on anything else
being alive.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Bounded so a dead database can't turn a busy service into unbounded
#: memory growth. At the flush interval below, this is >8 minutes of
#: buffering before anything is dropped — generous for an outage, finite
#: for a case where the flush thread itself has died.
_MAX_QUEUE = 5000

#: Drain the buffer this often, and also as soon as it reaches _BATCH_SIZE
#: (checked at enqueue time so a burst flushes promptly rather than waiting
#: out the whole interval).
_FLUSH_INTERVAL_SECONDS = 2.0
_BATCH_SIZE = 200


@dataclass
class _Row:
    ts: datetime
    role: str | None
    provider: str
    model: str
    kind: str
    caller: str
    units_in: int
    units_out: int
    reasoning_units: int
    seconds: float | None
    latency_ms: int | None
    cost_usd: float | None
    input_rate: float | None
    output_rate: float | None
    ok: bool
    error: str | None


#: kind is a closed set — the same list the HTTP ingest route validates
#: against (app/api/v1.py's AiUsagePush). Kept here too so record() rejects
#: (by dropping, not raising — see the module docstring) a bad kind from an
#: in-process caller the same way the HTTP route rejects one from outside.
VALID_KINDS = {"chat", "embedding", "stt", "tts", "vision", "prediction"}


@dataclass
class _Ledger:
    """Process-wide buffer + background flush thread. One instance, lazily started."""

    _queue: deque = field(default_factory=lambda: deque(maxlen=_MAX_QUEUE))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event)
    _dropped_total: int = field(default=0, init=False)
    _dropped_since_flush: int = field(default=0, init=False)

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(target=self._run, name="ai-ledger-flush", daemon=True)
            self._thread = t
            t.start()

    def enqueue(self, row: _Row) -> None:
        with self._lock:
            was_full = len(self._queue) == self._queue.maxlen
            self._queue.append(row)
            if was_full:
                # deque with maxlen silently drops the oldest item when full —
                # count it rather than let it vanish unremarked.
                self._dropped_total += 1
                self._dropped_since_flush += 1
        self._ensure_thread()

    def dropped_count(self) -> int:
        return self._dropped_total

    def _run(self) -> None:
        while not self._stop.is_set():
            self._flush_once()
            self._stop.wait(_FLUSH_INTERVAL_SECONDS)
        # Drain whatever is left on shutdown — best effort.
        self._flush_once()

    def _drain_batch(self) -> list[_Row]:
        batch: list[_Row] = []
        with self._lock:
            while self._queue and len(batch) < _BATCH_SIZE:
                batch.append(self._queue.popleft())
            dropped = self._dropped_since_flush
            self._dropped_since_flush = 0
        if dropped:
            logger.warning(
                "ai_ledger: dropped %d row(s) — queue was full (total dropped: %d)",
                dropped, self._dropped_total,
            )
        return batch

    def _flush_once(self) -> None:
        batch = self._drain_batch()
        if not batch:
            return
        try:
            self._write(batch)
        except Exception:
            # Never let a flush failure kill the thread — the batch is lost
            # (there is no re-queue: re-queuing risks an unbounded retry
            # storm against a database that is genuinely down), but the
            # thread keeps running so the NEXT flush isn't permanently dead.
            with self._lock:
                self._dropped_total += len(batch)
            logger.warning(
                "ai_ledger: flush of %d row(s) failed and was dropped "
                "(total dropped: %d)",
                len(batch), self._dropped_total, exc_info=True,
            )

    def _write(self, batch: list[_Row]) -> None:
        from app.db import get_db
        from app.models.ai_usage import AiUsage

        db = get_db()
        with db.session() as session:
            for row in batch:
                session.add(AiUsage(
                    ts=row.ts,
                    role=row.role,
                    provider=row.provider,
                    model=row.model,
                    kind=row.kind,
                    caller=row.caller,
                    units_in=row.units_in,
                    units_out=row.units_out,
                    reasoning_units=row.reasoning_units,
                    seconds=row.seconds,
                    latency_ms=row.latency_ms,
                    cost_usd=row.cost_usd,
                    input_rate=row.input_rate,
                    output_rate=row.output_rate,
                    ok=row.ok,
                    error=row.error,
                ))
            session.commit()

    def flush_sync(self) -> None:
        """Drain and write synchronously, for tests. Never raises."""
        try:
            self._flush_once()
        except Exception:
            logger.warning("ai_ledger: flush_sync failed", exc_info=True)


_LEDGER = _Ledger()


def record(
    *,
    provider: str,
    model: str,
    kind: str,
    caller: str,
    role: str | None = None,
    units_in: int = 0,
    units_out: int = 0,
    reasoning_units: int = 0,
    seconds: float | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    input_rate: float | None = None,
    output_rate: float | None = None,
    ok: bool = True,
    error: str | None = None,
    ts: datetime | None = None,
) -> None:
    """Record one AI call. NEVER raises into the caller — that is the contract.

    `cost_usd=None` means unknown, not free — a local call should pass
    `cost_usd=0.0` explicitly. See app/models/ai_usage.py's module docstring.
    """
    try:
        if kind not in VALID_KINDS:
            logger.warning("ai_ledger: dropping row with unknown kind=%r", kind)
            with _LEDGER._lock:
                _LEDGER._dropped_total += 1
            return
        row = _Row(
            ts=ts or datetime.now(timezone.utc),
            role=role,
            provider=provider,
            model=model,
            kind=kind,
            caller=caller,
            units_in=units_in,
            units_out=units_out,
            reasoning_units=reasoning_units,
            seconds=seconds,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            input_rate=input_rate,
            output_rate=output_rate,
            ok=ok,
            error=error,
        )
        _LEDGER.enqueue(row)
    except Exception:
        # Belt and braces: even a bug in this module's own bookkeeping must
        # not propagate into a caller mid-voice-reply.
        logger.warning("ai_ledger: record() failed unexpectedly", exc_info=True)


def record_llm_response(
    response,
    *,
    caller: str,
    kind: str = "chat",
    role: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Map a `coglib.llm.Response` onto a ledger row.

    Cost and rates come from `coglib.llm.MODELS` **at call time** — if the
    model isn't in that table (a provider/model this repo hasn't priced,
    e.g. the vision/transcription call sites that predate coglib.llm),
    `cost_usd`/`input_rate`/`output_rate` are recorded as NULL rather than
    guessed. This is the null-means-unknown rule applied to the one place a
    KeyError would otherwise be easy to hit (`Response.cost`'s property
    looks the model up in MODELS and raises if it's absent).

    Like `record()`, this NEVER raises — callers such as `app.algo.llm.AlgoLLM
    .ask()` call it inline, right after a successful (billable) LLM call, and
    a bug in this mapping must not turn that already-succeeded call into a
    failed one.
    """
    try:
        from coglib import llm as _llm

        input_rate = output_rate = None
        cost_usd = None
        spec = _llm.MODELS.get(response.model)
        if spec is not None:
            input_rate = spec.input_rate
            output_rate = spec.output_rate
            try:
                cost_usd = response.cost
            except Exception:
                logger.debug("ai_ledger: response.cost raised for model=%r", response.model)

        record(
            provider=response.provider,
            model=response.model,
            kind=kind,
            caller=caller,
            role=role,
            units_in=response.prompt_tokens,
            units_out=response.output_tokens,
            reasoning_units=response.reasoning_tokens,
            seconds=getattr(response, "secs", None),
            latency_ms=latency_ms if latency_ms is not None else (
                int(response.secs * 1000) if getattr(response, "secs", None) is not None else None
            ),
            cost_usd=cost_usd,
            input_rate=input_rate,
            output_rate=output_rate,
            ok=True,
        )
    except Exception:
        logger.warning("ai_ledger: record_llm_response() failed unexpectedly", exc_info=True)


def record_genai_usage(
    *,
    model: str,
    kind: str,
    caller: str,
    started: float,
    ok: bool,
    role: str | None = None,
    usage_metadata=None,
    error: str | None = None,
    seconds: float | None = None,
) -> None:
    """Record one `google.genai` call made outside `coglib.llm`.

    `seconds` is the audio duration for STT calls — the denominator for
    cost-per-minute, which token counts alone cannot give (added 2026-09-02;
    every earlier STT row has it NULL).

    `vision/client.py` and `transcription/gemini.py` both call `google.genai`
    directly (predating `coglib.llm`, and vision stays dependency-light on
    purpose — see its module docstring), so there's no `coglib.llm.Response`
    to hand `record_llm_response()`. This is the shared shape those two call
    sites use instead: pull token counts off the SDK's own
    `usage_metadata` (fields `prompt_token_count` / `candidates_token_count`
    / `thoughts_token_count`), and look up a rate from `coglib.llm.MODELS`
    only if this exact model string happens to be in that table — vision's
    and transcription's configured models are free-text config, not
    guaranteed to match one of coglib's entries, so `cost_usd` is NULL
    (unknown, not free) whenever it doesn't.

    `started` is a `time.time()` timestamp from just before the call, used
    to compute `latency_ms`. Never raises.
    """
    import time as _time

    try:
        from coglib import llm as _llm

        spec = _llm.MODELS.get(model)
        units_in = units_out = reasoning = 0
        if usage_metadata is not None:
            units_in = getattr(usage_metadata, "prompt_token_count", None) or 0
            units_out = getattr(usage_metadata, "candidates_token_count", None) or 0
            reasoning = getattr(usage_metadata, "thoughts_token_count", None) or 0

        cost_usd = input_rate = output_rate = None
        if spec is not None:
            input_rate, output_rate = spec.input_rate, spec.output_rate
            cost_usd = (
                units_in * input_rate + (units_out + reasoning) * output_rate
            ) / 1e6

        record(
            provider="google",
            model=model,
            kind=kind,
            caller=caller,
            role=role,
            units_in=units_in,
            units_out=units_out,
            reasoning_units=reasoning,
            latency_ms=int((_time.time() - started) * 1000),
            seconds=seconds,
            cost_usd=cost_usd,
            input_rate=input_rate,
            output_rate=output_rate,
            ok=ok,
            error=error,
        )
    except Exception:  # noqa: BLE001
        logger.debug("ai_ledger: record_genai_usage failed", exc_info=True)


def dropped_count() -> int:
    """Total rows dropped since process start (queue-full or flush failure)."""
    return _LEDGER.dropped_count()


def flush_sync() -> None:
    """Force a synchronous flush. For tests — production relies on the
    background thread."""
    _LEDGER.flush_sync()


def _reset_for_tests() -> None:
    """Test-only: drop the queue and stop the thread so tests don't leak
    daemon threads or state across each other."""
    global _LEDGER
    old = _LEDGER
    old._stop.set()
    _LEDGER = _Ledger()
