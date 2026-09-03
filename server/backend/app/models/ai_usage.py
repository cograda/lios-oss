"""The AI usage ledger — one row per AI call, including local ones.

Kernel-owned for the same reason `app/models/algo.py`'s three tables are:
this has to be readable across every integration, `comar-hub`, `scribe` and
HA at once ("one place to see all the cloud and local AI usage" — see
`vault/Projects/lios/Plans/AI Broker — Role Registry and Usage Ledger.md`),
so per-integration ownership would scatter the one aggregate view this table
exists to provide. Written through `app.services.ai_ledger`, never directly —
that module is the only place that gets to decide "never raise, never block".

Two nullability decisions carry the whole design, and both are explained at
length in the AI Broker plan; restated briefly here because a column
definition is exactly the place a future edit would get them backwards:

  `cost_usd` is nullable, and **null means "unknown", never "free".** A local
  call costs `0.0` — recorded explicitly, because an omitted row would make a
  cloud→local swap look like the workload vanishing rather than getting
  cheaper. A flat-subscription provider (Nabu Casa STT/TTS) has no per-call
  price at all, and *that* is what null is for. Conflating the two would
  quietly understate spend.

  `input_rate`/`output_rate` are nullable and store the rate **used at call
  time**, not derived from `cost_usd` after the fact. `coglib.llm.MODELS` is
  hand-maintained and admits its own rates go stale; if only the derived cost
  were kept, correcting a rate later would silently rewrite every historical
  total. Storing the rate alongside the cost makes every row reproducible and
  turns a rate correction into a deliberate, visible act instead of an
  invisible one.

`role` is nullable and unused until the registry chunk (build order step 2)
fills it in — every row here is written with `role=None` for now. Recording
the shape before there's anything to put in it means the registry chunk is a
column write, not a migration.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from coglib import Base


class AiUsage(Base):
    """One AI call — cloud or local, chat or embedding or STT/TTS/vision.

    `caller` is a free-text namespace string (`"algo:solar_forecast"`,
    `"integration:vision"`, `"hub:bridge"`, `"scribe"`) rather than a foreign
    key, because callers include things with no row anywhere in this
    database — a comar-hub container, a laptop-local scribe process. The
    `(caller, ts)` index is what makes "what has X been costing" a fast query
    without needing a join.
    """

    __tablename__ = "ai_usage"
    __table_args__ = (
        Index("ix_ai_usage_ts", "ts"),
        Index("ix_ai_usage_caller_ts", "caller", "ts"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    #: Reserved for the role-registry chunk (build order step 2). Every row
    #: written by this chunk carries `role=None` — see module docstring.
    role: Mapped[str | None] = mapped_column(Text, nullable=True)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    #: chat | embedding | stt | tts | vision | prediction
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    #: Free-text namespace, e.g. "algo:solar_forecast", "integration:vision".
    caller: Mapped[str] = mapped_column(String(128), nullable=False)

    units_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    units_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Audio duration, for STT/TTS. NULL for anything not time-based.
    seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: NULL means unknown, never free. See module docstring.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    #: The $/1M rate used at call time, not derived after the fact. See
    #: module docstring — this is what keeps a rate correction from
    #: silently rewriting history.
    input_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    output_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
